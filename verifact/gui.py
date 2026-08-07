"""
gui.py
======

WHAT THIS FILE DOES
-------------------
Builds the entire native desktop window with tkinter. No web technology is
involved anywhere: this is a real OS window drawn by Tk.

LAYOUT
------
    +--------------------------------------------------------------+
    | File   Edit   Help                          <- menu bar       |
    +--------------------------------------------------------------+
    | [ Paste Text ] [ Batch Files ]              <- ttk.Notebook   |
    |                                                              |
    |  Tab 1: big text box + "Verify Text" + colour-coded log      |
    |  Tab 2: file list + Treeview results + detail pane           |
    |                                                              |
    +--------------------------------------------------------------+
    | status text                      [progress bar]  backend info |
    +--------------------------------------------------------------+

THREADING MODEL (important)
---------------------------
Tk is NOT thread safe: only the main thread may touch widgets. So:

  * The heavy work (parsing files, spaCy, database round-trips) runs on a
    ``threading.Thread``.
  * That worker never calls a widget method directly. It pushes messages onto
    a ``queue.Queue``.
  * The main thread runs :meth:`VeriFactApp._drain_queue` every 60 ms via
    ``after()``, pops those messages and updates the widgets.

This is the standard, crash-free way to keep a tkinter GUI responsive, and it
is why the window never freezes during a long batch job.

DRAG AND DROP
-------------
Plain tkinter has no drag-and-drop. If the optional ``tkinterdnd2`` package is
installed we register the file list as a drop target and real Explorer drops
work. If it is not installed the app still runs and simply tells the user to
use the Import Files button instead.
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
import traceback
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List, Optional

from . import APP_NAME, __version__
from .compare import (COMPARE_COLOURS, STATUS_CHANGED, STATUS_ONLY_A,
                      STATUS_ONLY_B, STATUS_SAME, ComparisonReport,
                      compare_files)
from .config_manager import ConfigManager
from .database import DataSourceManager
from .dialogs import (AliasManager, DatabaseBrowser, SuggestionDialog,
                      ask_new_value, show_about, show_help)
from .exporters import export_auto, export_comparison
from .file_parsers import SUPPORTED_EXTENSIONS, parse_file
from .formulas import FormulaEngine
from .nlp_engine import NLPEngine, RegionRegistry
from .suggestions import (VERDICT_ACCEPTED, VERDICT_IGNORED, VERDICT_REJECTED,
                          AliasStore, SuggestionEngine)
from .verifier import (STATUS_FAIL, STATUS_PASS, STATUS_WARN, ValidationRules,
                       VerificationResult, Verifier, summarise)

# Optional drag-and-drop support.
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore
    DND_AVAILABLE = True
except Exception:  # pragma: no cover
    DND_FILES = None  # type: ignore
    TkinterDnD = None  # type: ignore
    DND_AVAILABLE = False


# Colours used for Treeview row tags and the text log.
ROW_COLOURS = {
    STATUS_PASS: ("#E4F6E4", "#0B6B24"),
    STATUS_FAIL: ("#FCE4E4", "#A11212"),
    STATUS_WARN: ("#FFF6DC", "#8A6100"),
}

SAMPLE_TEXT = (
    "Quick demo - press \"Verify Text\".\n\n"
    "In Q3 2024 our European active users reached 984.5k, which was a solid "
    "result. Global revenue for the same quarter came in at $4.8M and churn "
    "held at 2.3%. North American active users were 2,450 in Q3 2024, and "
    "last quarter total sales were 5.25M.\n"
)


class VeriFactApp:
    """The application. Construct it, then call :meth:`run`."""

    # ------------------------------------------------------------------
    # CONSTRUCTION
    # ------------------------------------------------------------------
    def __init__(self, base_dir: str) -> None:
        self.base_dir = base_dir

        # --- backend objects ------------------------------------------
        self.config = ConfigManager(base_dir)
        self.data = DataSourceManager(self.config)
        # Learned metric corrections live in the same SQLite file.
        self.alias_store = AliasStore(self.data.local.path)
        self.suggester = SuggestionEngine(
            self.alias_store, threshold=self.config.suggestion_threshold)
        self.formula_engine = FormulaEngine(self.config.derived_metrics)
        self.engine = NLPEngine(
            reference_year=int(self.config.get(
                "verification", "demo_reference_year", default=2024) or 0),
            reference_quarter=int(self.config.get(
                "verification", "demo_reference_quarter", default=4) or 4),
        )
        self.verifier = Verifier(
            self.data,
            tolerance_percent=self.config.tolerance_percent,
            default_to_latest=bool(self.config.get(
                "verification", "default_to_latest_period", default=True)),
            formula_engine=self.formula_engine,
            validation=ValidationRules(self.config.validation_rules),
        )
        # Region vocabulary + metric vocabulary come from the DATA, not from
        # any hardcoded list. This is the call that replaces REGION_KEYWORDS.
        self._refresh_vocabularies()

        # --- state -----------------------------------------------------
        self.results: List[VerificationResult] = []      # batch tab results
        self.text_results: List[VerificationResult] = [] # text tab results
        self.pending_files: List[str] = []
        self.row_index: Dict[str, VerificationResult] = {}  # tree iid -> result
        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.cancel_flag = threading.Event()

        # --- Compare tab state -----------------------------------------
        self.compare_path_a: Optional[str] = None
        self.compare_path_b: Optional[str] = None
        self.compare_report: Optional[ComparisonReport] = None
        self.compare_row_index: Dict[str, Any] = {}

        # --- context carousel state ------------------------------------
        # Ordered tree iids for the file currently shown in the detail pane,
        # plus our position within it. Prev/Next walk this list.
        self.carousel_iids: List[str] = []
        self.carousel_pos: int = -1
        self.carousel_file: str = ""

        # --- smart-suggest state ---------------------------------------
        # The worker thread cannot open a dialog, so it posts a request and
        # blocks on this Event until the main thread stores an answer.
        self._suggest_event = threading.Event()
        self._suggest_answer: Optional[Tuple[str, str]] = None
        self._suggest_prompts_used = 0
        self._suggest_stopped = False

        # --- window ----------------------------------------------------
        self.root = TkinterDnD.Tk() if DND_AVAILABLE else tk.Tk()
        self._build_window()
        self._build_menu()
        self._build_body()
        self._build_status_bar()
        self._apply_startup_messages()

        # Start the queue pump that carries worker messages to the widgets.
        self._after_id: Optional[str] = self.root.after(60, self._drain_queue)

    # ------------------------------------------------------------------
    # VOCABULARY: regions and metrics are LEARNED, never hardcoded
    # ------------------------------------------------------------------
    def _refresh_vocabularies(self) -> None:
        """Load region labels and metric names from the live data sources.

        This is the replacement for the deleted ``REGION_KEYWORDS`` constant.
        Priority:
          1. ``SELECT DISTINCT region`` across external + local databases.
          2. ``regions.fallback`` from config.json when that returns nothing.
          3. An empty registry, which simply disables region matching - a
             neutral default rather than a wrong guess.

        Also registers every known metric name (plus derived-metric keys) with
        the extractor, so a warehouse column that was never added to
        METRIC_KEYWORD_MAP is still recognised in prose.
        """
        labels: List[str] = []
        source = "config fallback"

        if self.config.load_regions_from_db:
            try:
                labels = self.data.distinct_regions()
                source = "database"
            except Exception:
                labels = []

        if not labels:
            labels = self.config.region_fallback
            source = "config fallback"

        registry = RegionRegistry(
            labels,
            synonyms=self.config.region_synonyms,
            min_token_length=self.config.region_min_token_length,
        )
        registry.source = source if labels else "empty"
        self.regions = registry
        self.engine.set_regions(registry)

        # Metric vocabulary: DB metrics + derived-metric names.
        try:
            db_metrics = list(self.data.known_metrics())
        except Exception:
            db_metrics = []
        formula_names = self.formula_engine.all_names()
        self.engine.register_metrics(db_metrics + formula_names)

        # Learned corrections take precedence over everything.
        self.engine.set_aliases({
            key: value for key, (value, verdict)
            in getattr(self.alias_store, "_cache", {}).items()
            if verdict == VERDICT_ACCEPTED
        })

        # Candidate pool for fuzzy suggestions.
        candidates = sorted(set(
            db_metrics + formula_names +
            list(self.engine.mapper.mapping.values())))
        self.suggester.set_candidates(candidates)

    def _build_window(self) -> None:
        self.root.title(f"{APP_NAME}  v{__version__}")
        width = int(self.config.get("ui", "window_width", default=1180) or 1180)
        height = int(self.config.get("ui", "window_height", default=760) or 760)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(940, 600)   # resizable, with a sensible floor

        style = ttk.Style(self.root)
        theme = str(self.config.get("ui", "theme", default="clam"))
        if theme in style.theme_names():
            style.theme_use(theme)
        style.configure("Treeview", rowheight=23)
        style.configure("Heading.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("Status.TLabel", padding=(8, 3))
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # MENU BAR
    # ------------------------------------------------------------------
    def _build_menu(self) -> None:
        """File / Edit / Help, exactly as specified."""
        menubar = tk.Menu(self.root)

        # --- File ------------------------------------------------------
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Open Files...", accelerator="Ctrl+O",
                              command=self.action_import_files)
        file_menu.add_command(label="Open Text File into Tab 1...",
                              command=self.action_open_text_file)
        file_menu.add_separator()
        file_menu.add_command(label="Export Results...", accelerator="Ctrl+S",
                              command=self.action_export_results)
        file_menu.add_command(label="Export as CSV",
                              command=lambda: self.action_export_results(".csv"))
        file_menu.add_command(label="Export as Excel (colour coded)",
                              command=lambda: self.action_export_results(".xlsx"))
        file_menu.add_command(label="Export as JSON log",
                              command=lambda: self.action_export_results(".json"))
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        # --- Edit ------------------------------------------------------
        edit_menu = tk.Menu(menubar, tearoff=0)
        edit_menu.add_command(label="Cut", accelerator="Ctrl+X",
                              command=lambda: self._text_event("<<Cut>>"))
        edit_menu.add_command(label="Copy", accelerator="Ctrl+C",
                              command=lambda: self._text_event("<<Copy>>"))
        edit_menu.add_command(label="Paste", accelerator="Ctrl+V",
                              command=lambda: self._text_event("<<Paste>>"))
        edit_menu.add_command(label="Select All", accelerator="Ctrl+A",
                              command=self.action_select_all)
        edit_menu.add_separator()
        edit_menu.add_command(label="Paste from Clipboard into Text Tab",
                              command=self.action_paste_clipboard)
        edit_menu.add_command(label="Insert Sample Claims",
                              command=self.action_insert_sample)
        edit_menu.add_separator()
        edit_menu.add_command(label="Clear Text Tab", command=self.action_clear_text)
        edit_menu.add_command(label="Clear Batch Results",
                              command=self.action_clear_results)
        edit_menu.add_separator()
        edit_menu.add_command(label="Learned Metric Corrections...",
                              command=self.action_manage_aliases)
        edit_menu.add_command(label="Reload config.json", command=self.action_reload_config)
        edit_menu.add_command(label="Open config.json in Editor",
                              command=self.action_open_config)
        menubar.add_cascade(label="Edit", menu=edit_menu)

        # --- Help ------------------------------------------------------
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="How to Use", command=lambda: show_help(self.root))
        help_menu.add_command(label="Browse Truth Database",
                              command=self.action_browse_database)
        help_menu.add_command(label="Region Vocabulary...",
                              command=self.action_show_regions)
        help_menu.add_command(label="Derived Metric Formulas...",
                              command=self.action_show_formulas)
        help_menu.add_command(label="Migrate Legacy Region Labels...",
                              command=self.action_migrate_regions)
        help_menu.add_command(label="Backend Diagnostics",
                              command=self.action_diagnostics)
        help_menu.add_separator()
        help_menu.add_command(label=f"About {APP_NAME}",
                              command=lambda: show_about(self.root))
        menubar.add_cascade(label="Help", menu=help_menu)

        self.root.config(menu=menubar)

        # Keyboard shortcuts
        self.root.bind("<Control-o>", lambda e: self.action_import_files())
        self.root.bind("<Control-s>", lambda e: self.action_export_results())
        self.root.bind("<Control-Return>", lambda e: self.action_verify_text())

    # ------------------------------------------------------------------
    # BODY: NOTEBOOK WITH TWO TABS
    # ------------------------------------------------------------------
    def _build_body(self) -> None:
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        self._build_text_tab()
        self._build_file_tab()
        self._build_compare_tab()

    # ---------------------- TAB 1: PASTE TEXT --------------------------
    def _build_text_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="  1. Paste Text  ")

        ttk.Label(tab, text="Paste claims from ChatGPT, an email or a report:",
                  style="Heading.TLabel").pack(anchor="w")

        # A vertical paned window lets the user drag the divider between the
        # input box and the results log.
        pane = ttk.PanedWindow(tab, orient="vertical")
        pane.pack(fill="both", expand=True, pady=(6, 0))

        # --- input box ---------------------------------------------------
        input_frame = ttk.Frame(pane)
        self.text_input = tk.Text(input_frame, wrap="word", height=12,
                                  font=("Consolas", 10), undo=True)
        input_scroll = ttk.Scrollbar(input_frame, orient="vertical",
                                     command=self.text_input.yview)
        self.text_input.configure(yscrollcommand=input_scroll.set)
        self.text_input.pack(side="left", fill="both", expand=True)
        input_scroll.pack(side="left", fill="y")
        pane.add(input_frame, weight=2)

        # --- buttons ------------------------------------------------------
        buttons = ttk.Frame(tab)
        buttons.pack(fill="x", pady=(8, 4))
        self.btn_verify_text = ttk.Button(buttons, text="Verify Text",
                                          style="Accent.TButton",
                                          command=self.action_verify_text)
        self.btn_verify_text.pack(side="left")
        ttk.Button(buttons, text="Paste from Clipboard",
                   command=self.action_paste_clipboard).pack(side="left", padx=6)
        ttk.Button(buttons, text="Insert Sample",
                   command=self.action_insert_sample).pack(side="left")
        ttk.Button(buttons, text="Clear",
                   command=self.action_clear_text).pack(side="left", padx=6)
        ttk.Button(buttons, text="Send Results to Batch Table",
                   command=self.action_promote_text_results).pack(side="left")
        ttk.Label(buttons, text="   (Ctrl+Enter to verify)",
                  foreground="#666666").pack(side="left")

        # --- results log ---------------------------------------------------
        log_frame = ttk.Frame(pane)
        ttk.Label(log_frame, text="Results log",
                  style="Heading.TLabel").pack(anchor="w")
        self.text_log = tk.Text(log_frame, wrap="word", height=14, state="disabled",
                                font=("Consolas", 9), background="#FCFCFC")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical",
                                   command=self.text_log.yview)
        self.text_log.configure(yscrollcommand=log_scroll.set)
        self.text_log.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="left", fill="y")
        pane.add(log_frame, weight=3)

        # Colour tags used when writing verdicts into the log.
        for status, (background, foreground) in ROW_COLOURS.items():
            self.text_log.tag_configure(status, background=background,
                                        foreground=foreground)
        self.text_log.tag_configure("header", font=("Consolas", 9, "bold"))
        self.text_log.tag_configure("dim", foreground="#777777")

        self.text_input.insert("1.0", SAMPLE_TEXT)

    # ---------------------- TAB 2: BATCH FILES -------------------------
    def _build_file_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="  2. Batch Files  ")

        # --- toolbar ------------------------------------------------------
        toolbar = ttk.Frame(tab)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Import Files",
                   command=self.action_import_files).pack(side="left")
        ttk.Button(toolbar, text="Import Folder",
                   command=self.action_import_folder).pack(side="left", padx=6)
        ttk.Button(toolbar, text="Remove Selected",
                   command=self.action_remove_selected_file).pack(side="left")
        ttk.Button(toolbar, text="Clear List",
                   command=self.action_clear_files).pack(side="left", padx=6)
        self.btn_verify_files = ttk.Button(toolbar, text="Verify All Files",
                                           style="Accent.TButton",
                                           command=self.action_verify_files)
        self.btn_verify_files.pack(side="left", padx=(18, 0))
        self.btn_cancel = ttk.Button(toolbar, text="Cancel", state="disabled",
                                     command=self.action_cancel)
        self.btn_cancel.pack(side="left", padx=6)
        ttk.Button(toolbar, text="Export Results...",
                   command=self.action_export_results).pack(side="right")

        drop_hint = ("Drag files here, or use Import Files"
                     if DND_AVAILABLE else
                     "Tip: install 'tkinterdnd2' to enable drag-and-drop")
        ttk.Label(tab, text=drop_hint, foreground="#666666").pack(anchor="w", pady=(6, 2))

        # --- main split: file list | results ------------------------------
        outer = ttk.PanedWindow(tab, orient="horizontal")
        outer.pack(fill="both", expand=True)

        # left: queued files
        left = ttk.Frame(outer)
        ttk.Label(left, text="Files to verify", style="Heading.TLabel").pack(anchor="w")
        self.file_list = tk.Listbox(left, selectmode="extended", height=8,
                                    font=("Consolas", 9), activestyle="dotbox")
        file_scroll = ttk.Scrollbar(left, orient="vertical",
                                    command=self.file_list.yview)
        self.file_list.configure(yscrollcommand=file_scroll.set)
        self.file_list.pack(side="left", fill="both", expand=True)
        file_scroll.pack(side="left", fill="y")
        outer.add(left, weight=1)

        # right: results treeview over a detail pane
        right = ttk.PanedWindow(outer, orient="vertical")
        outer.add(right, weight=4)

        tree_frame = ttk.Frame(right)
        columns = ("filename", "line", "claimed", "metric", "timeframe",
                   "actual", "status", "source")
        headings = {
            "filename": "Filename", "line": "Line/Row", "claimed": "Claimed Value",
            "metric": "Metric", "timeframe": "Timeframe", "actual": "Actual Value",
            "status": "Status", "source": "Source",
        }
        widths = {"filename": 150, "line": 70, "claimed": 110, "metric": 130,
                  "timeframe": 110, "actual": 110, "status": 80, "source": 240}

        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings",
                                 selectmode="browse")
        for column in columns:
            self.tree.heading(
                column, text=headings[column],
                command=lambda c=column: self._sort_tree(c))
            self.tree.column(
                column, width=widths[column],
                anchor="e" if column in ("line", "claimed", "actual") else "w")

        tree_y = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        tree_x = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=tree_y.set, xscrollcommand=tree_x.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        tree_y.grid(row=0, column=1, sticky="ns")
        tree_x.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        right.add(tree_frame, weight=3)

        # Row colouring by verdict.
        for status, (background, foreground) in ROW_COLOURS.items():
            self.tree.tag_configure(status, background=background,
                                    foreground=foreground)

        # Click -> detail pane ; right-click -> context menu
        self.tree.bind("<<TreeviewSelect>>", self._on_row_selected)
        self.tree.bind("<Button-3>", self._on_row_right_click)
        self.tree.bind("<Double-1>", lambda e: self.action_copy_query())

        # detail pane, with the CONTEXT CAROUSEL along its header
        detail_frame = ttk.Frame(right)

        carousel = ttk.Frame(detail_frame)
        carousel.pack(fill="x")
        ttk.Label(carousel, text="Detail - full original context",
                  style="Heading.TLabel").pack(side="left")

        # The carousel lets a user step through every claim in ONE file
        # without hunting for rows in a table of thousands. Selecting any row
        # (re)builds the ring from that row's file.
        self.btn_prev = ttk.Button(carousel, text="< Previous", width=11,
                                   state="disabled",
                                   command=lambda: self.action_carousel(-1))
        self.btn_next = ttk.Button(carousel, text="Next >", width=9,
                                   state="disabled",
                                   command=lambda: self.action_carousel(1))
        self.carousel_var = tk.StringVar(value="no claim selected")
        ttk.Button(carousel, text="Last", width=6,
                   command=lambda: self.action_carousel_jump(-1)).pack(side="right", padx=(4, 0))
        self.btn_next.pack(side="right", padx=2)
        ttk.Label(carousel, textvariable=self.carousel_var,
                  foreground="#555555", width=30, anchor="center").pack(side="right")
        self.btn_prev.pack(side="right", padx=2)
        ttk.Button(carousel, text="First", width=6,
                   command=lambda: self.action_carousel_jump(0)).pack(side="right", padx=(0, 4))

        body = ttk.Frame(detail_frame)
        body.pack(fill="both", expand=True)
        self.detail = tk.Text(body, wrap="word", height=9, state="disabled",
                              font=("Consolas", 9), background="#FCFCFC")
        detail_scroll = ttk.Scrollbar(body, orient="vertical",
                                      command=self.detail.yview)
        self.detail.configure(yscrollcommand=detail_scroll.set)
        self.detail.pack(side="left", fill="both", expand=True)
        detail_scroll.pack(side="left", fill="y")
        for status, (background, foreground) in ROW_COLOURS.items():
            self.detail.tag_configure(status, foreground=foreground,
                                      font=("Consolas", 9, "bold"))
        self.detail.tag_configure("label", font=("Consolas", 9, "bold"))
        right.add(detail_frame, weight=2)

        # Keyboard shortcuts for the carousel (Alt+Left / Alt+Right).
        self.root.bind("<Alt-Left>", lambda e: self.action_carousel(-1))
        self.root.bind("<Alt-Right>", lambda e: self.action_carousel(1))

        self._build_context_menu()
        self._register_drag_and_drop(tab)

    # ---------------------- TAB 3: COMPARE -----------------------------
    def _build_compare_tab(self) -> None:
        """Two-file diff. Deliberately never touches the database.

        This tab answers "what changed between the draft and the final?" -
        a question about two documents, not about truth. Keeping it entirely
        DB-free means it works even with no database configured at all.
        """
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="  3. Compare  ")

        ttk.Label(tab,
                  text="Compare two documents - draft vs final. "
                       "No database is used; this is a pure difference report.",
                  style="Heading.TLabel").pack(anchor="w")

        # --- file slots ---------------------------------------------------
        slots = ttk.Frame(tab)
        slots.pack(fill="x", pady=(8, 4))

        self.compare_a_var = tk.StringVar(value="(no file - click Choose, or drag one here)")
        self.compare_b_var = tk.StringVar(value="(no file - click Choose, or drag one here)")

        for index, (label, variable, setter) in enumerate((
                ("Source A", self.compare_a_var, lambda: self.action_pick_compare("a")),
                ("Source B", self.compare_b_var, lambda: self.action_pick_compare("b")))):
            row = ttk.Frame(slots)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=f"{label}:", width=10,
                      font=("Segoe UI", 9, "bold")).pack(side="left")
            ttk.Button(row, text="Choose...", command=setter).pack(side="left")
            ttk.Label(row, textvariable=variable, foreground="#444444"
                      ).pack(side="left", padx=8)

        # --- actions ------------------------------------------------------
        actions = ttk.Frame(tab)
        actions.pack(fill="x", pady=(6, 4))
        self.btn_compare = ttk.Button(actions, text="Compare Files",
                                      style="Accent.TButton",
                                      command=self.action_run_compare)
        self.btn_compare.pack(side="left")
        ttk.Button(actions, text="Swap A/B",
                   command=self.action_swap_compare).pack(side="left", padx=6)
        ttk.Button(actions, text="Clear",
                   command=self.action_clear_compare).pack(side="left")
        ttk.Button(actions, text="Export Comparison...",
                   command=self.action_export_compare).pack(side="right")

        self.compare_summary_var = tk.StringVar(value="")
        ttk.Label(tab, textvariable=self.compare_summary_var,
                  foreground="#333333").pack(anchor="w", pady=(4, 2))

        # --- results table -------------------------------------------------
        table = ttk.Frame(tab)
        table.pack(fill="both", expand=True)

        columns = ("metric", "region", "timeframe", "value_a", "value_b",
                   "delta", "delta_pct", "status")
        headings = {"metric": "Metric", "region": "Region",
                    "timeframe": "Timeframe", "value_a": "Source A Value",
                    "value_b": "Source B Value", "delta": "Delta",
                    "delta_pct": "Delta %", "status": "Status"}
        widths = {"metric": 150, "region": 110, "timeframe": 120,
                  "value_a": 130, "value_b": 130, "delta": 120,
                  "delta_pct": 90, "status": 100}

        self.compare_tree = ttk.Treeview(table, columns=columns,
                                         show="headings", selectmode="browse")
        for column in columns:
            self.compare_tree.heading(column, text=headings[column])
            self.compare_tree.column(
                column, width=widths[column],
                anchor="e" if column in ("value_a", "value_b", "delta", "delta_pct")
                else "w")

        y_scroll = ttk.Scrollbar(table, orient="vertical",
                                 command=self.compare_tree.yview)
        self.compare_tree.configure(yscrollcommand=y_scroll.set)
        self.compare_tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)

        for status, colour in COMPARE_COLOURS.items():
            self.compare_tree.tag_configure(status, background=colour)

        self.compare_tree.bind("<<TreeviewSelect>>", self._on_compare_selected)

        # --- detail pane ---------------------------------------------------
        ttk.Label(tab, text="Detail - the sentence each value came from",
                  style="Heading.TLabel").pack(anchor="w", pady=(8, 2))
        self.compare_detail = tk.Text(tab, wrap="word", height=7,
                                      state="disabled", font=("Consolas", 9),
                                      background="#FCFCFC")
        self.compare_detail.pack(fill="x")
        self.compare_detail.tag_configure("label", font=("Consolas", 9, "bold"))

        self._register_compare_drop(tab)

    def _register_compare_drop(self, widget: tk.Misc) -> None:
        """Allow dropping files straight onto the Compare tab.

        Dropping one file fills the first empty slot; dropping two fills both.
        """
        if not DND_AVAILABLE:
            return
        try:
            for target in (widget, self.compare_tree):
                target.drop_target_register(DND_FILES)      # type: ignore[attr-defined]
                target.dnd_bind("<<Drop>>", self._on_compare_drop)  # type: ignore[attr-defined]
        except Exception:
            pass

    def _on_compare_drop(self, event) -> None:
        paths = [p for p in self.root.tk.splitlist(event.data) if os.path.isfile(p)]
        for path in paths[:2]:
            if not self.compare_path_a:
                self._set_compare_slot("a", path)
            elif not self.compare_path_b:
                self._set_compare_slot("b", path)
            else:
                # Both full - a further drop replaces A and shifts A into B.
                self._set_compare_slot("b", self.compare_path_a)
                self._set_compare_slot("a", path)
        self.notebook.select(2)

    def _build_context_menu(self) -> None:
        """Right-click menu on a results row - the audit-transparency feature."""
        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="Correct the Claim in Database",
                                      command=self.action_correct_in_database)
        self.context_menu.add_command(label="Copy Source Query",
                                      command=self.action_copy_query)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Copy Full Context",
                                      command=self.action_copy_context)
        self.context_menu.add_command(label="Copy Row as Text",
                                      command=self.action_copy_row)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Browse Truth Database",
                                      command=self.action_browse_database)

    def _register_drag_and_drop(self, widget: tk.Misc) -> None:
        """Wire up Explorer drag-and-drop when tkinterdnd2 is present."""
        if not DND_AVAILABLE:
            return
        try:
            for target in (widget, self.file_list, self.tree):
                target.drop_target_register(DND_FILES)   # type: ignore[attr-defined]
                target.dnd_bind("<<Drop>>", self._on_drop)  # type: ignore[attr-defined]
        except Exception:
            pass  # never let an optional feature break startup

    def _on_drop(self, event) -> None:
        """Handle a file drop. Tk gives us a brace-quoted, space-separated list."""
        paths = self.root.tk.splitlist(event.data)
        self._add_files([p for p in paths if os.path.isfile(p)])
        self.notebook.select(1)

    # ------------------------------------------------------------------
    # STATUS BAR
    # ------------------------------------------------------------------
    def _build_status_bar(self) -> None:
        bar = ttk.Frame(self.root, relief="sunken")
        bar.pack(fill="x", side="bottom")

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(bar, textvariable=self.status_var, style="Status.TLabel",
                  anchor="w").pack(side="left", fill="x", expand=True)

        self.progress = ttk.Progressbar(bar, mode="determinate", length=210)
        self.progress.pack(side="left", padx=8, pady=3)

        self.backend_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.backend_var, style="Status.TLabel",
                  anchor="e", foreground="#333333").pack(side="right")

    def _apply_startup_messages(self) -> None:
        """Populate the status bar and warn about any configuration problems."""
        self.backend_var.set(
            f"{self.data.backend_status()}   |   NLP: {self.engine.backend_name}"
            f"   |   Regions: {len(self.regions.labels)} ({self.regions.source})"
        )
        self.set_status("Ready. Paste text and press Verify Text, or import files.")

        notes: List[str] = list(self.config.load_errors)
        if self.engine.backend_note:
            notes.append(self.engine.backend_note)
        # Surface bad formulas immediately - a silently disabled formula would
        # otherwise show up much later as a puzzling AMBER row.
        for name, error in self.formula_engine.errors.items():
            notes.append(f"Formula '{name}' disabled: {error}")
        if self.regions.is_empty:
            notes.append(
                "No region labels found in the database or config fallback - "
                "region matching is disabled and lookups will ignore region.")
        if not self.alias_store.available and self.config.suggestions_enabled:
            notes.append("Learned-corrections table unavailable: "
                         f"{self.alias_store.last_error}")
        if self.config.external_enabled and not self.data.external.available:
            notes.append("External database is enabled but unreachable: "
                         f"{self.data.external.last_error}\n"
                         "Veri-Fact will use the local SQLite database instead.")
        if notes:
            # Show as a non-blocking log entry rather than a modal wall of text.
            self._log("Startup notes:\n  - " + "\n  - ".join(notes) + "\n\n", "dim")

    def set_status(self, message: str) -> None:
        self.status_var.set(message)

    # ------------------------------------------------------------------
    # WORKER -> GUI MESSAGE PUMP
    # ------------------------------------------------------------------
    def _post(self, kind: str, *payload: Any) -> None:
        """Called FROM THE WORKER THREAD. Never touches a widget."""
        self.ui_queue.put((kind, payload))

    def _drain_queue(self) -> None:
        """Runs on the MAIN thread every 60 ms; applies queued UI updates."""
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()

                if kind == "status":
                    self.set_status(payload[0])
                elif kind == "progress":
                    done, total = payload
                    self.progress.configure(
                        maximum=max(1, total), value=done)
                elif kind == "log":
                    self._log(payload[0], payload[1] if len(payload) > 1 else None)
                elif kind == "rows":
                    self._append_rows(payload[0])
                elif kind == "text_done":
                    self._finish_text_run(payload[0])
                elif kind == "files_done":
                    self._finish_file_run(payload[0], payload[1])
                elif kind == "compare_done":
                    self._finish_compare(payload[0])
                elif kind == "suggest":
                    # The worker is blocked waiting for the user's answer.
                    self._handle_suggestion_request(payload[0], payload[1])
                elif kind == "error":
                    messagebox.showerror(APP_NAME, payload[0])
                elif kind == "warn":
                    messagebox.showwarning(APP_NAME, payload[0])
                elif kind == "busy":
                    self._set_busy(bool(payload[0]))
        except queue.Empty:
            pass
        finally:
            # Reschedule ONLY while the window still exists. Without this
            # guard, destroying the root mid-cycle leaves a pending 'after'
            # callback that fires against a dead interpreter and prints
            # 'invalid command name ..._drain_queue' to the console.
            try:
                if self.root.winfo_exists():
                    self._after_id = self.root.after(60, self._drain_queue)
            except tk.TclError:
                pass

    def _set_busy(self, busy: bool) -> None:
        """Disable the action buttons while a job runs."""
        state = "disabled" if busy else "normal"
        self.btn_verify_text.configure(state=state)
        self.btn_verify_files.configure(state=state)
        self.btn_cancel.configure(state="normal" if busy else "disabled")
        if not busy:
            self.progress.configure(value=0)

    def _busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    # ------------------------------------------------------------------
    # LOGGING HELPERS (text tab)
    # ------------------------------------------------------------------
    def _log(self, message: str, tag: Optional[str] = None) -> None:
        self.text_log.configure(state="normal")
        if tag:
            self.text_log.insert("end", message, tag)
        else:
            self.text_log.insert("end", message)
        self.text_log.see("end")
        self.text_log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.text_log.configure(state="normal")
        self.text_log.delete("1.0", "end")
        self.text_log.configure(state="disabled")

    # ==================================================================
    # ACTION: VERIFY PASTED TEXT
    # ==================================================================
    def action_verify_text(self) -> None:
        if self._busy():
            messagebox.showinfo(APP_NAME, "A verification job is already running.")
            return

        text = self.text_input.get("1.0", "end").strip()
        if not text:
            messagebox.showinfo(APP_NAME, "The text box is empty. Paste some claims first.")
            return

        self._clear_log()
        self._log(f"Verifying pasted text  ({len(text)} characters)\n", "header")
        self._log(f"Extractor: {self.engine.backend_name}   |   "
                  f"{self.data.backend_status()}\n"
                  f"Tolerance: {self.verifier.tolerance}%\n"
                  f"{'-' * 78}\n", "dim")

        self.cancel_flag.clear()
        self._set_busy(True)
        self.worker = threading.Thread(target=self._worker_verify_text,
                                       args=(text,), daemon=True)
        self.worker.start()

    def _worker_verify_text(self, text: str) -> None:
        """WORKER THREAD: extract + verify pasted text."""
        try:
            self._post("status", "Extracting numbers from text...")
            claims = self.engine.extract_claims(text)

            if not claims:
                self._post("text_done", [])
                return

            results: List[VerificationResult] = []
            total = len(claims)
            for index, claim in enumerate(claims, start=1):
                if self.cancel_flag.is_set():
                    break
                results.append(self._verify_with_suggestions(claim, "(pasted text)"))
                self._post("progress", index, total)
                self._post("status", f"Verifying claim {index} of {total}...")

            self._post("text_done", results)
        except Exception:
            self._post("error", f"Verification failed:\n\n{traceback.format_exc()}")
            self._post("text_done", [])

    def _finish_text_run(self, results: List[VerificationResult]) -> None:
        """MAIN THREAD: render the text-tab results into the log."""
        self.text_results = results
        self._set_busy(False)

        if not results:
            self._log("\nNo verifiable numeric claims were found.\n", STATUS_WARN)
            self._log("Tip: mention a metric word such as users, revenue, churn, "
                      "sales or margin next to the number.\n", "dim")
            self.set_status("Finished - nothing to verify.")
            return

        for index, item in enumerate(results, start=1):
            self._log(f"\n[{index:>3}] {item.status}  ", item.status)
            self._log(f"{item.metric_column or item.metric_text}  "
                      f"({item.timeframe})\n")
            self._log(f"      claimed : {item.claimed_display}"
                      f"   -> compared as {_num(item.claimed_value)}\n")
            self._log(f"      actual  : {_num(item.actual_value)}"
                      f"   diff {_pct(item.difference_percent)}\n")
            self._log(f"      source  : {item.source}\n", "dim")
            self._log(f"      note    : {item.reason}\n", "dim")
            self._log(f"      context : ...{item.context}...\n", "dim")

        tally = summarise(results)
        self._log(f"\n{'-' * 78}\n", "dim")
        self._log(f"SUMMARY: {tally['TOTAL']} claims  |  ", "header")
        self._log(f"{tally[STATUS_PASS]} PASS", STATUS_PASS)
        self._log("  |  ")
        self._log(f"{tally[STATUS_FAIL]} FAIL", STATUS_FAIL)
        self._log("  |  ")
        self._log(f"{tally[STATUS_WARN]} WARNING", STATUS_WARN)
        self._log("\n")
        self.set_status(
            f"Text verified: {tally['TOTAL']} claims - "
            f"{tally[STATUS_PASS]} pass, {tally[STATUS_FAIL]} fail, "
            f"{tally[STATUS_WARN]} warning."
        )

    def action_promote_text_results(self) -> None:
        """Copy the text-tab results into the batch table so they can be exported."""
        if not self.text_results:
            messagebox.showinfo(APP_NAME, "Verify some text first.")
            return
        self._append_rows(self.text_results)
        self.notebook.select(1)
        self.set_status(f"Added {len(self.text_results)} text results to the batch table.")

    # ==================================================================
    # ACTION: FILE IMPORT + BATCH VERIFY
    # ==================================================================
    def action_import_files(self) -> None:
        types = [
            ("All supported", " ".join(f"*{e}" for e in SUPPORTED_EXTENSIONS)),
            ("Text and logs", "*.txt *.log"),
            ("Spreadsheets", "*.csv *.xlsx *.xls"),
            ("JSON", "*.json"),
            ("All files", "*.*"),
        ]
        paths = filedialog.askopenfilenames(
            title="Import files to verify", filetypes=types)
        self._add_files(list(paths))
        if paths:
            self.notebook.select(1)

    def action_import_folder(self) -> None:
        """Add every supported file in a folder (non-recursive, keeps it predictable)."""
        folder = filedialog.askdirectory(title="Import every supported file in a folder")
        if not folder:
            return
        found = [
            os.path.join(folder, name)
            for name in sorted(os.listdir(folder))
            if os.path.splitext(name)[1].lower() in SUPPORTED_EXTENSIONS
            and os.path.isfile(os.path.join(folder, name))
        ]
        if not found:
            messagebox.showinfo(APP_NAME, "No supported files found in that folder.")
            return
        self._add_files(found)
        self.notebook.select(1)

    def action_open_text_file(self) -> None:
        """File > Open a single text file straight into the paste-text tab."""
        path = filedialog.askopenfilename(
            title="Open a text file into the Paste Text tab",
            filetypes=[("Text files", "*.txt *.log *.md"), ("All files", "*.*")])
        if not path:
            return
        try:
            from .file_parsers import read_text_file
            content = read_text_file(path)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open the file:\n{exc}")
            return
        self.text_input.delete("1.0", "end")
        self.text_input.insert("1.0", content)
        self.notebook.select(0)
        self.set_status(f"Loaded {os.path.basename(path)} into the text tab.")

    def _add_files(self, paths: List[str]) -> None:
        added = 0
        for path in paths:
            normalised = os.path.abspath(path)
            if normalised in self.pending_files:
                continue
            self.pending_files.append(normalised)
            self.file_list.insert("end", normalised)
            added += 1
        if added:
            self.set_status(f"{added} file(s) added - {len(self.pending_files)} queued.")

    def action_remove_selected_file(self) -> None:
        for index in reversed(self.file_list.curselection()):
            self.file_list.delete(index)
            del self.pending_files[index]
        self.set_status(f"{len(self.pending_files)} file(s) queued.")

    def action_clear_files(self) -> None:
        self.pending_files.clear()
        self.file_list.delete(0, "end")
        self.set_status("File list cleared.")

    def action_verify_files(self) -> None:
        if self._busy():
            messagebox.showinfo(APP_NAME, "A verification job is already running.")
            return
        if not self.pending_files:
            messagebox.showinfo(APP_NAME,
                                "No files queued. Use Import Files or drag files in.")
            return

        self.cancel_flag.clear()
        self._set_busy(True)
        files = list(self.pending_files)
        self.worker = threading.Thread(target=self._worker_verify_files,
                                       args=(files,), daemon=True)
        self.worker.start()

    def _worker_verify_files(self, files: List[str]) -> None:
        """WORKER THREAD: parse and verify every queued file.

        A failure in one file is reported and skipped; the batch continues.
        """
        collected: List[VerificationResult] = []
        problems: List[str] = []
        total_files = len(files)

        try:
            for file_index, path in enumerate(files, start=1):
                if self.cancel_flag.is_set():
                    problems.append("Cancelled by user.")
                    break

                name = os.path.basename(path)
                self._post("status", f"Processing file {file_index} of {total_files}: {name}")
                self._post("progress", file_index - 1, total_files)

                parsed = parse_file(path, self.engine)

                if not parsed.ok:
                    problems.append(f"{name}: {parsed.error}")
                    warn = self.verifier.empty_result(name, parsed.error)
                    collected.append(warn)
                    self._post("rows", [warn])
                    continue

                if not parsed.claims:
                    warn = self.verifier.empty_result(
                        name, "No numeric claims could be extracted from this file.")
                    collected.append(warn)
                    self._post("rows", [warn])
                    continue

                batch: List[VerificationResult] = []
                claim_total = len(parsed.claims)
                for claim_index, claim in enumerate(parsed.claims, start=1):
                    if self.cancel_flag.is_set():
                        break
                    batch.append(self._verify_with_suggestions(claim, name))
                    if claim_index % 25 == 0 or claim_index == claim_total:
                        self._post("status",
                                   f"Processing file {file_index} of {total_files}: "
                                   f"{name} - claim {claim_index}/{claim_total}")

                collected.extend(batch)
                self._post("rows", batch)
                self._post("progress", file_index, total_files)

            self._post("files_done", collected, problems)
        except Exception:
            self._post("error", f"Batch verification failed:\n\n{traceback.format_exc()}")
            self._post("files_done", collected, problems)

    #: Treeview rows inserted per main-thread slice. Tk has no bulk-insert API,
    #: so a single 4,000-row loop blocks the event loop for seconds and the
    #: window visibly freezes. Inserting in slices and yielding via after()
    #: keeps the UI repainting throughout.
    _INSERT_CHUNK = 250

    def _append_rows(self, rows: List[VerificationResult]) -> None:
        """MAIN THREAD: insert result rows into the Treeview with colour tags.

        Large batches are inserted incrementally. ``after(0, ...)`` returns
        control to Tk between slices so the progress bar, status bar and the
        window itself stay responsive even on a 4,000-claim file.
        """
        if not rows:
            return

        # Track membership with a set of id()s. The previous
        # ``if item not in self.results`` did a linear scan with dataclass
        # __eq__ on every insert - O(n^2), which alone cost seconds at 4k rows.
        if not hasattr(self, "_result_ids"):
            self._result_ids = set()

        pending = list(rows)

        def insert_slice() -> None:
            chunk, remaining = pending[:self._INSERT_CHUNK], pending[self._INSERT_CHUNK:]
            for item in chunk:
                try:
                    iid = self.tree.insert("", "end", values=item.as_tuple(),
                                           tags=(item.status,))
                except tk.TclError:
                    return  # window closed mid-insert
                self.row_index[iid] = item
                if id(item) not in self._result_ids:
                    self._result_ids.add(id(item))
                    self.results.append(item)

            pending[:] = remaining
            if pending:
                try:
                    self.root.after(0, insert_slice)
                except tk.TclError:
                    pass

        insert_slice()

    def _finish_file_run(self, results: List[VerificationResult],
                         problems: List[str]) -> None:
        self._set_busy(False)

        # Summarise the WORKER's authoritative list, not self.results.
        # Treeview rows are inserted asynchronously in slices (see
        # _append_rows), so at this moment self.results may still be filling
        # up - summarising it would under-report the totals on large batches.
        tally = summarise(results) if results else summarise(self.results)
        self.set_status(
            f"Batch complete: {tally['TOTAL']} claims - {tally[STATUS_PASS]} pass, "
            f"{tally[STATUS_FAIL]} fail, {tally[STATUS_WARN]} warning."
        )
        if problems:
            preview = "\n".join(f"  - {p}" for p in problems[:12])
            more = f"\n  ...and {len(problems) - 12} more" if len(problems) > 12 else ""
            messagebox.showwarning(
                f"{APP_NAME} - some files had problems",
                "The batch finished, but these files were skipped or partially "
                f"processed:\n\n{preview}{more}")

    def action_cancel(self) -> None:
        if self._busy():
            self.cancel_flag.set()
            self.set_status("Cancelling after the current claim...")

    def action_clear_results(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.row_index.clear()
        self.results.clear()
        # Must be cleared too, or previously-seen results would be silently
        # skipped when re-verified (they'd appear in the tree but never make it
        # back into self.results, so exports would come out short).
        if hasattr(self, "_result_ids"):
            self._result_ids.clear()
        self.carousel_iids = []
        self.carousel_pos = -1
        self.carousel_file = ""
        self._update_carousel_label()
        self._set_detail("")
        self.set_status("Results cleared.")

    # ==================================================================
    # SMART SUGGEST  (worker asks, main thread shows the dialog)
    # ==================================================================
    def _handle_suggestion_request(self, suggestion, alternatives) -> None:
        """MAIN THREAD: show the Yes/No/Ignore dialog and release the worker.

        Tk forbids widget access from a background thread, so the worker posts
        a 'suggest' message and blocks on ``self._suggest_event``. We answer
        here and set the event, unblocking it. The try/finally guarantees the
        worker is ALWAYS released - a crash in the dialog must never deadlock
        a verification run.
        """
        try:
            dialog = SuggestionDialog(self.root, suggestion, alternatives)
            verdict = dialog.result or "ignore"
            chosen = dialog.chosen_metric or suggestion.suggested_metric

            if verdict == "stop":
                self._suggest_stopped = True
                self._suggest_answer = ("ignore", "")
            elif verdict == "yes":
                self.alias_store.remember(suggestion.source_text, chosen,
                                          VERDICT_ACCEPTED)
                # Teach the live extractor immediately so later claims in the
                # same run benefit without waiting for a restart.
                self.engine.mapper.aliases[
                    suggestion.source_text.strip().lower()] = chosen
                self._suggest_answer = ("yes", chosen)
            elif verdict == "no":
                self.alias_store.remember(suggestion.source_text, chosen,
                                          VERDICT_REJECTED)
                self._suggest_answer = ("no", "")
            else:
                self.alias_store.remember(suggestion.source_text,
                                          chosen or "(ignored)", VERDICT_IGNORED)
                self._suggest_answer = ("ignore", "")
        except Exception:
            self._suggest_answer = ("ignore", "")
        finally:
            self._suggest_event.set()

    def _verify_with_suggestions(self, claim, filename: str) -> VerificationResult:
        """WORKER THREAD: verify a claim, offering a fix if the metric is unknown.

        Flow:
          1. Verify normally.
          2. If the result is AMBER *because the metric was not recognised*,
             fuzzy-match the text and (optionally) ask the user.
          3. On acceptance, re-verify with the corrected metric so the row
             shows a real PASS/FAIL instead of a warning.

        Any failure here falls back to the plain result - suggestions are a
        convenience and must never break verification.
        """
        result = self.verifier.verify_claim(claim, filename=filename)

        if result.status != STATUS_WARN or claim.metric_column:
            return result

        probe = (claim.metric_text or claim.source_label or "").strip()
        if not probe:
            return result

        try:
            accepted = self._ask_suggestion(probe)
        except Exception:
            return result

        if not accepted:
            return result

        # Re-run with the user's correction applied.
        try:
            claim.metric_column = accepted
            corrected = self.verifier.verify_claim(claim, filename=filename)
            corrected.suggested_metric = accepted
            corrected.reason = (f"Metric resolved to '{accepted}' from "
                                f"'{probe}' by your confirmation. "
                                f"{corrected.reason}")
            return corrected
        except Exception:
            return result

    def _ask_suggestion(self, source_text: str) -> Optional[str]:
        """WORKER THREAD: request a suggestion decision, blocking for the answer.

        Returns the accepted metric name, or ``None`` if the user declined.
        Returns ``None`` immediately when prompting is disabled, the budget is
        exhausted, or the user pressed "Stop asking".
        """
        if not self.config.suggestions_enabled or self._suggest_stopped:
            return None

        # A remembered decision needs no prompt at all.
        remembered = self.alias_store.resolve(source_text)
        if remembered:
            self.alias_store.record_hit(source_text)
            return remembered

        if not self.config.suggestion_prompt:
            return None
        if self._suggest_prompts_used >= self.config.max_prompts_per_run:
            return None

        suggestion = self.suggester.best_suggestion(source_text)
        if suggestion is None:
            return None

        alternatives = self.suggester.top_suggestions(source_text, limit=5)

        self._suggest_prompts_used += 1
        self._suggest_answer = None
        self._suggest_event.clear()
        self._post("suggest", suggestion, alternatives)

        # Wait for the main thread. The timeout is a deadlock guard: if the
        # GUI is destroyed mid-run we give up rather than hanging forever.
        if not self._suggest_event.wait(timeout=300):
            return None

        answer = self._suggest_answer or ("ignore", "")
        return answer[1] if answer[0] == "yes" and answer[1] else None

    # ==================================================================
    # COMPARE TAB
    # ==================================================================
    def _set_compare_slot(self, slot: str, path: Optional[str]) -> None:
        """Point slot 'a' or 'b' at a file (or clear it) and update the label."""
        variable = self.compare_a_var if slot == "a" else self.compare_b_var
        if path:
            absolute = os.path.abspath(path)
            if slot == "a":
                self.compare_path_a = absolute
            else:
                self.compare_path_b = absolute
            variable.set(os.path.basename(absolute))
        else:
            if slot == "a":
                self.compare_path_a = None
            else:
                self.compare_path_b = None
            variable.set("(no file - click Choose, or drag one here)")

    def action_pick_compare(self, slot: str) -> None:
        types = [
            ("All supported", " ".join(f"*{e}" for e in SUPPORTED_EXTENSIONS)),
            ("Text and logs", "*.txt *.log"),
            ("Spreadsheets", "*.csv *.xlsx *.xls"),
            ("JSON", "*.json"),
            ("All files", "*.*"),
        ]
        path = filedialog.askopenfilename(
            title=f"Choose Source {slot.upper()}", filetypes=types)
        if path:
            self._set_compare_slot(slot, path)

    def action_swap_compare(self) -> None:
        a, b = self.compare_path_a, self.compare_path_b
        self._set_compare_slot("a", b)
        self._set_compare_slot("b", a)

    def action_clear_compare(self) -> None:
        self._set_compare_slot("a", None)
        self._set_compare_slot("b", None)
        self.compare_tree.delete(*self.compare_tree.get_children())
        self.compare_row_index.clear()
        self.compare_report = None
        self.compare_summary_var.set("")
        self._set_compare_detail("")

    def action_run_compare(self) -> None:
        """Diff the two chosen files on a worker thread."""
        if self._busy():
            messagebox.showinfo(APP_NAME, "A job is already running.")
            return
        if not (self.compare_path_a and self.compare_path_b):
            messagebox.showinfo(APP_NAME, "Choose two files first.")
            return

        self.compare_tree.delete(*self.compare_tree.get_children())
        self.compare_row_index.clear()
        self._set_compare_detail("")
        self.cancel_flag.clear()
        self._set_busy(True)
        self.worker = threading.Thread(
            target=self._worker_compare,
            args=(self.compare_path_a, self.compare_path_b), daemon=True)
        self.worker.start()

    def _worker_compare(self, path_a: str, path_b: str) -> None:
        """WORKER THREAD: parse and diff two files."""
        try:
            self._post("status", f"Comparing {os.path.basename(path_a)} "
                                 f"with {os.path.basename(path_b)}...")
            self._post("progress", 1, 3)
            report = compare_files(path_a, path_b, self.engine,
                                   tolerance_percent=0.0)
            self._post("progress", 3, 3)
            self._post("compare_done", report)
        except Exception:
            self._post("error", f"Comparison failed:\n\n{traceback.format_exc()}")
            self._post("compare_done", ComparisonReport(
                ok=False, error="Unexpected failure - see the error dialog."))

    def _finish_compare(self, report: ComparisonReport) -> None:
        """MAIN THREAD: render the comparison result."""
        self._set_busy(False)
        self.compare_report = report

        if not report.ok:
            self.compare_summary_var.set(report.error)
            messagebox.showerror(APP_NAME, report.error)
            self.set_status("Comparison failed.")
            return

        for row in report.rows:
            iid = self.compare_tree.insert("", "end", values=row.as_tuple(),
                                           tags=(row.status,))
            self.compare_row_index[iid] = row

        tally = report.summary()
        self.compare_summary_var.set(
            f"A = {report.file_a}    B = {report.file_b}    |    "
            f"{tally['TOTAL']} aligned metrics: "
            f"{tally[STATUS_CHANGED]} changed, {tally[STATUS_SAME]} identical, "
            f"{tally[STATUS_ONLY_A]} only in A, {tally[STATUS_ONLY_B]} only in B"
        )
        self.set_status(f"Comparison complete: {tally[STATUS_CHANGED]} value(s) changed.")

        if report.warnings:
            messagebox.showwarning(APP_NAME, "\n".join(report.warnings))

    def _on_compare_selected(self, _event=None) -> None:
        selection = self.compare_tree.selection()
        if not selection:
            return
        row = self.compare_row_index.get(selection[0])
        if row is None:
            return

        self.compare_detail.configure(state="normal")
        self.compare_detail.delete("1.0", "end")
        self.compare_detail.insert("end", f"{row.metric}  ({row.region or 'no region'}, "
                                          f"{row.timeframe or 'no timeframe'})  "
                                          f"- {row.status}\n\n", "label")
        self.compare_detail.insert("end", "Source A", "label")
        self.compare_detail.insert(
            "end", f" (line {row.line_a}): {row.context_a or '(not present)'}\n\n")
        self.compare_detail.insert("end", "Source B", "label")
        self.compare_detail.insert(
            "end", f" (line {row.line_b}): {row.context_b or '(not present)'}\n")
        self.compare_detail.configure(state="disabled")

    def _set_compare_detail(self, text: str) -> None:
        self.compare_detail.configure(state="normal")
        self.compare_detail.delete("1.0", "end")
        self.compare_detail.insert("1.0", text)
        self.compare_detail.configure(state="disabled")

    def action_export_compare(self) -> None:
        """Save the comparison table as CSV / XLSX / JSON."""
        if not (self.compare_report and self.compare_report.rows):
            messagebox.showinfo(APP_NAME, "Run a comparison first.")
            return
        path = filedialog.asksaveasfilename(
            title="Export comparison",
            defaultextension=".xlsx",
            initialfile=f"verifact_comparison_{datetime.now():%Y%m%d_%H%M%S}.xlsx",
            filetypes=[("Excel report (colour coded)", "*.xlsx"),
                       ("CSV file", "*.csv"), ("JSON log", "*.json")])
        if not path:
            return
        ok, message = export_comparison(self.compare_report, path)
        if ok:
            self.set_status(message)
            messagebox.showinfo(APP_NAME, f"{message}\n\n{path}")
        else:
            messagebox.showerror(APP_NAME, message)

    # ==================================================================
    # CONTEXT CAROUSEL
    # ==================================================================
    def _rebuild_carousel(self, iid: str) -> None:
        """Collect every row belonging to the same file as ``iid``.

        The carousel is scoped to ONE file on purpose: stepping from the last
        claim of report_a.csv into the first claim of report_b.json would be
        disorienting. Within a file the order is exactly the table order, so
        Next follows the document.
        """
        item = self.row_index.get(iid)
        if item is None:
            self.carousel_iids = []
            self.carousel_pos = -1
            self._update_carousel_label()
            return

        filename = item.filename
        if filename != self.carousel_file or iid not in self.carousel_iids:
            self.carousel_file = filename
            self.carousel_iids = [
                candidate for candidate in self.tree.get_children("")
                if self.row_index.get(candidate) is not None
                and self.row_index[candidate].filename == filename
            ]
        try:
            self.carousel_pos = self.carousel_iids.index(iid)
        except ValueError:
            self.carousel_pos = -1
        self._update_carousel_label()

    def _update_carousel_label(self) -> None:
        total = len(self.carousel_iids)
        if total == 0 or self.carousel_pos < 0:
            self.carousel_var.set("no claim selected")
            self.btn_prev.configure(state="disabled")
            self.btn_next.configure(state="disabled")
            return
        name = self.carousel_file
        if len(name) > 22:
            name = name[:19] + "..."
        self.carousel_var.set(f"{name}  -  claim {self.carousel_pos + 1} of {total}")
        self.btn_prev.configure(state="normal" if self.carousel_pos > 0 else "disabled")
        self.btn_next.configure(
            state="normal" if self.carousel_pos < total - 1 else "disabled")

    def action_carousel(self, step: int) -> None:
        """Move Previous/Next through the current file's claims."""
        if not self.carousel_iids:
            return
        new_pos = self.carousel_pos + step
        if not (0 <= new_pos < len(self.carousel_iids)):
            return
        self._goto_carousel(new_pos)

    def action_carousel_jump(self, position: int) -> None:
        """Jump to the first (0) or last (-1) claim in the file."""
        if not self.carousel_iids:
            return
        index = 0 if position == 0 else len(self.carousel_iids) - 1
        self._goto_carousel(index)

    def _goto_carousel(self, index: int) -> None:
        """Select and reveal the row at ``index``, updating the detail pane."""
        self.carousel_pos = index
        iid = self.carousel_iids[index]
        # Selecting the row fires <<TreeviewSelect>>, which renders the detail
        # pane; ``see`` scrolls it into view so the highlight is visible.
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self.tree.see(iid)
        self._update_carousel_label()

    # ==================================================================
    # TREEVIEW INTERACTION
    # ==================================================================
    def _selected_result(self) -> Optional[VerificationResult]:
        selection = self.tree.selection()
        if not selection:
            return None
        return self.row_index.get(selection[0])

    def _on_row_selected(self, _event=None) -> None:
        """Clicking a row shows its complete original context underneath."""
        selection = self.tree.selection()
        if not selection:
            return
        item = self.row_index.get(selection[0])
        if item is None:
            return
        self._render_detail(item)
        # Re-scope the carousel to this row's file so Prev/Next walk it.
        self._rebuild_carousel(selection[0])

    def _on_row_right_click(self, event) -> None:
        """Select the row under the cursor, then pop the context menu."""
        row_id = self.tree.identify_row(event.y)
        if row_id:
            self.tree.selection_set(row_id)
            self.tree.focus(row_id)
            self._render_detail(self.row_index[row_id])
            try:
                self.context_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.context_menu.grab_release()

    def _set_detail(self, text: str) -> None:
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")

    def _render_detail(self, item: VerificationResult) -> None:
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")

        self.detail.insert("end", f"{item.status}", item.status)
        self.detail.insert("end", f"   {item.filename}  (line/row {item.line_no})\n\n")

        pairs = [
            ("Claim as written", item.claimed_display),
            ("Compared value", _num(item.claimed_value)),
            ("Metric", f"{item.metric_column or '(unmapped)'}  "
                       f"[matched words: {item.metric_text or '-'}]"),
            ("Region", item.region or "(not stated)"),
            ("Timeframe", item.timeframe or "-"),
            ("Database value", _num(item.actual_value)),
            ("Difference", _pct(item.difference_percent)),
            ("Interpretation", item.interpretation or "-"),
            ("Source", item.source or "-"),
            ("Reason", item.reason),
        ]
        for label, value in pairs:
            self.detail.insert("end", f"{label:>16} : ", "label")
            self.detail.insert("end", f"{value}\n")

        self.detail.insert("end", "\nOriginal context\n", "label")
        self.detail.insert("end", f"{item.context or '(none captured)'}\n")

        if item.rendered_query():
            self.detail.insert("end", "\nSQL used\n", "label")
            self.detail.insert("end", f"{item.rendered_query()}\n")

        self.detail.configure(state="disabled")

    def _sort_tree(self, column: str) -> None:
        """Click a column heading to sort. Numeric columns sort numerically."""
        rows = [(self.tree.set(iid, column), iid) for iid in self.tree.get_children("")]

        def key(pair):
            raw = pair[0].replace(",", "")
            try:
                return (0, float(raw))
            except ValueError:
                return (1, pair[0].lower())

        descending = getattr(self, "_sort_desc", {}).get(column, False)
        rows.sort(key=key, reverse=descending)
        for position, (_, iid) in enumerate(rows):
            self.tree.move(iid, "", position)
        if not hasattr(self, "_sort_desc"):
            self._sort_desc = {}
        self._sort_desc[column] = not descending

    # ==================================================================
    # CONTEXT MENU ACTIONS (audit transparency)
    # ==================================================================
    def action_correct_in_database(self) -> None:
        """Update the local SQLite truth value for the selected row.

        Two situations are handled:
          * the row matched a SQLite record  -> UPDATE that record.
          * the row was a WARNING with no match -> offer to INSERT a new record
            so the metric exists next time.
        """
        item = self._selected_result()
        if item is None:
            messagebox.showinfo(APP_NAME, "Select a result row first.")
            return

        if item.db_row_id is not None:
            new_value = ask_new_value(self.root,
                                      item.metric_column or item.metric_text,
                                      item.actual_value, item.claimed_value)
            if new_value is None:
                return
            if self.data.local.update_value(int(item.db_row_id), new_value):
                item.actual_value = new_value
                item.difference_percent = (
                    Verifier._percent_difference(item.claimed_value or 0.0, new_value)
                )
                item.status = (STATUS_PASS
                               if item.difference_percent < self.verifier.tolerance
                               else STATUS_FAIL)
                item.reason = (f"Database corrected to {new_value} by the user on "
                               f"{datetime.now():%Y-%m-%d %H:%M}.")
                self._refresh_selected_row(item)
                self.set_status(f"Database row {item.db_row_id} updated to {new_value}.")
            else:
                messagebox.showerror(APP_NAME, "Could not update the SQLite row.")
            return

        # No matching row - offer to create one.
        if not item.metric_column:
            messagebox.showinfo(
                APP_NAME,
                "This row has no recognised metric, so there is nothing to correct.\n\n"
                "Add a keyword to METRIC_KEYWORD_MAP in verifact/nlp_engine.py first.")
            return

        if not messagebox.askyesno(
                APP_NAME,
                f"'{item.metric_column}' has no row for {item.timeframe}.\n\n"
                f"Create a new database row with the claimed value "
                f"{_num(item.claimed_value)}?"):
            return

        new_value = ask_new_value(self.root, item.metric_column, None, item.claimed_value)
        if new_value is None:
            return

        quarter = "Q4"
        year = 2024
        parsed = self.engine.timeframe_parser.parse(item.timeframe or "")
        if parsed.quarter:
            quarter = parsed.quarter
        if parsed.year:
            year = parsed.year

        row_id = self.data.local.insert_row(
            item.metric_column, item.region or "Global", quarter, year, new_value)
        if row_id is None:
            messagebox.showerror(APP_NAME, "Could not insert the new SQLite row.")
            return

        item.db_row_id = row_id
        item.actual_value = new_value
        item.source = f"SQLite:{self.data.local.table}#{row_id} (user added)"
        item.difference_percent = Verifier._percent_difference(
            item.claimed_value or 0.0, new_value)
        item.status = (STATUS_PASS
                       if item.difference_percent < self.verifier.tolerance
                       else STATUS_FAIL)
        item.reason = f"New truth row created by the user on {datetime.now():%Y-%m-%d %H:%M}."
        self._refresh_selected_row(item)
        self.set_status(f"Inserted {item.metric_column} = {new_value} ({quarter} {year}).")

    def _refresh_selected_row(self, item: VerificationResult) -> None:
        selection = self.tree.selection()
        if selection:
            self.tree.item(selection[0], values=item.as_tuple(), tags=(item.status,))
            self._render_detail(item)

    def action_copy_query(self) -> None:
        """Put the exact SQL used for this row on the clipboard."""
        item = self._selected_result()
        if item is None:
            messagebox.showinfo(APP_NAME, "Select a result row first.")
            return
        query = item.rendered_query()
        if not query:
            messagebox.showinfo(APP_NAME,
                                "No SQL was run for this row (the metric was never "
                                "identified, so the database was not queried).")
            return
        self._to_clipboard(query)
        self.set_status("Source query copied to the clipboard.")

    def action_copy_context(self) -> None:
        item = self._selected_result()
        if item is None:
            return
        self._to_clipboard(item.context or "")
        self.set_status("Context copied to the clipboard.")

    def action_copy_row(self) -> None:
        item = self._selected_result()
        if item is None:
            return
        text = "\n".join(f"{k}: {v}" for k, v in item.to_dict().items())
        self._to_clipboard(text)
        self.set_status("Row copied to the clipboard.")

    def _to_clipboard(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()

    # ==================================================================
    # EXPORT
    # ==================================================================
    def action_export_results(self, force_extension: Optional[str] = None) -> None:
        """Save every result row to CSV, colour-coded XLSX, or a JSON log."""
        rows = self.results or self.text_results
        if not rows:
            messagebox.showinfo(APP_NAME, "There are no results to export yet.")
            return

        # Rows land in the table asynchronously (see _append_rows). Exporting
        # mid-insert would silently write a partial file, which is exactly the
        # kind of quiet data loss this tool exists to catch.
        if self._busy():
            messagebox.showinfo(
                APP_NAME,
                "Verification is still running - please wait for it to finish "
                "before exporting, so the report contains every row.")
            return

        types = [("CSV file", "*.csv"),
                 ("Excel report (colour coded)", "*.xlsx"),
                 ("JSON log", "*.json")]
        if force_extension:
            types.sort(key=lambda t: not t[1].endswith(force_extension))

        default_name = f"verifact_results_{datetime.now():%Y%m%d_%H%M%S}"
        path = filedialog.asksaveasfilename(
            title="Export verification results",
            defaultextension=force_extension or ".csv",
            initialfile=default_name + (force_extension or ".csv"),
            filetypes=types)
        if not path:
            return

        metadata = {
            "Tool version": __version__,
            "Tolerance percent": self.verifier.tolerance,
            "NLP backend": self.engine.backend_name,
            "Data backend": self.data.backend_status(),
            "Local database": self.data.local.path,
            "Files processed": len({r.filename for r in rows}),
        }
        ok, message = export_auto(rows, path, metadata)
        if ok:
            self.set_status(message)
            messagebox.showinfo(APP_NAME, f"{message}\n\n{path}")
        else:
            messagebox.showerror(APP_NAME, message)

    # ==================================================================
    # EDIT / HELP ACTIONS
    # ==================================================================
    def _text_event(self, event: str) -> None:
        """Forward a virtual clipboard event to whichever widget has focus."""
        widget = self.root.focus_get()
        if widget is not None:
            try:
                widget.event_generate(event)
            except tk.TclError:
                pass

    def action_select_all(self) -> None:
        widget = self.root.focus_get()
        if isinstance(widget, tk.Text):
            widget.tag_add("sel", "1.0", "end-1c")
        elif isinstance(widget, tk.Listbox):
            widget.select_set(0, "end")

    def action_paste_clipboard(self) -> None:
        """Explicit 'paste raw text from the clipboard' support."""
        try:
            content = self.root.clipboard_get()
        except tk.TclError:
            messagebox.showinfo(APP_NAME, "The clipboard is empty or holds non-text data.")
            return
        self.notebook.select(0)
        self.text_input.insert("insert", content)
        self.set_status(f"Pasted {len(content)} characters from the clipboard.")

    def action_insert_sample(self) -> None:
        self.notebook.select(0)
        self.text_input.insert("end", "\n" + SAMPLE_TEXT)

    def action_clear_text(self) -> None:
        self.text_input.delete("1.0", "end")
        self._clear_log()
        self.text_results.clear()
        self.set_status("Text tab cleared.")

    def action_reload_config(self) -> None:
        """Re-read config.json and rebuild the backends without restarting."""
        self.config.load()
        self.data = DataSourceManager(self.config)
        self.alias_store = AliasStore(self.data.local.path)
        self.suggester = SuggestionEngine(
            self.alias_store, threshold=self.config.suggestion_threshold)
        self.formula_engine = FormulaEngine(self.config.derived_metrics)
        self.verifier = Verifier(
            self.data,
            tolerance_percent=self.config.tolerance_percent,
            default_to_latest=bool(self.config.get(
                "verification", "default_to_latest_period", default=True)),
            formula_engine=self.formula_engine,
            validation=ValidationRules(self.config.validation_rules))
        # Re-learn regions and metrics from the (possibly new) data source.
        self._refresh_vocabularies()
        self._suggest_prompts_used = 0
        self._suggest_stopped = False
        self._apply_startup_messages()

        notes = [f"Configuration reloaded.", "", self.data.backend_status(),
                 f"Regions: {self.regions.describe()}",
                 f"Formulas: {len(self.formula_engine.definitions)} active"]
        if self.formula_engine.errors:
            notes.append(f"WARNING: {len(self.formula_engine.errors)} formula(s) "
                         f"rejected - see Help > Derived Metric Formulas.")
        messagebox.showinfo(APP_NAME, "\n".join(notes))

    def action_open_config(self) -> None:
        """Open config.json in the OS default editor (Notepad on Windows)."""
        try:
            os.startfile(self.config.path)  # type: ignore[attr-defined]
        except AttributeError:
            messagebox.showinfo(APP_NAME, f"Edit this file:\n{self.config.path}")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open the file:\n{exc}")

    def action_browse_database(self) -> None:
        DatabaseBrowser(self.root, self.data.local)

    def action_manage_aliases(self) -> None:
        """Show the corrections the user has taught the smart-suggest engine."""
        AliasManager(self.root, self.alias_store)

    def action_show_regions(self) -> None:
        """Explain where the region vocabulary came from - no hardcoded list."""
        from .dialogs import show_text_window
        lines = [
            "REGION VOCABULARY",
            "",
            "Veri-Fact does not ship a list of world regions. It learns the",
            "vocabulary from your data at startup:",
            "",
            "    SELECT DISTINCT region FROM " + self.data.local.table,
            "",
            f"Source        : {self.regions.source}",
            f"Labels loaded : {len(self.regions.labels)}",
            "",
            "Recognised labels:",
        ]
        lines += [f"  - {label}" for label in self.regions.labels] or ["  (none)"]

        synonyms = self.config.region_synonyms
        lines += ["", "Prose synonyms from config.json:"]
        if synonyms:
            for canonical, aliases in synonyms.items():
                lines.append(f"  {canonical} <- {', '.join(map(str, aliases))}")
        else:
            lines.append("  (none configured)")

        lines += [
            "",
            "To add a region: insert a row with that region into the database,",
            "or add it to regions.fallback in config.json. To recognise extra",
            "words for an existing label, use regions.synonyms, e.g.",
            '    "synonyms": { "Region_A": ["emea", "europe"] }',
        ]
        show_text_window(self.root, "Region Vocabulary", "\n".join(lines), height=28)

    def action_show_formulas(self) -> None:
        """List derived metrics and any formulas rejected as unsafe/invalid."""
        from .dialogs import show_text_window
        lines = ["DERIVED METRIC FORMULAS", "",
                 "Defined in the 'derived_metrics' block of config.json.",
                 "Expressions are parsed and whitelisted - never eval()'d.", ""]

        if self.formula_engine.definitions:
            lines.append("Active formulas:")
            for name in self.formula_engine.all_names():
                lines.append(f"  {name}")
                lines.append(f"      = {self.formula_engine.expression_for(name)}")
                lines.append(f"      needs: "
                             f"{', '.join(self.formula_engine.constituents(name))}")
        else:
            lines.append("No formulas are defined.")

        if self.formula_engine.errors:
            lines += ["", "REJECTED (fix these in config.json):"]
            for name, error in self.formula_engine.errors.items():
                lines.append(f"  {name}: {error}")

        lines += ["", "Allowed in expressions:",
                  "  numbers, metric names, + - * / // % **",
                  "  min() max() abs() round() sum() avg() sqrt()",
                  "", "Example:",
                  '  "total_revenue": "region_a_revenue + region_b_revenue"']
        show_text_window(self.root, "Derived Metric Formulas",
                         "\n".join(lines), height=28)

    def action_migrate_regions(self) -> None:
        """Offer to rename legacy demo region labels to neutral placeholders.

        Strictly opt-in and preview-first: we show exactly which rows would
        change before touching anything, because a user may legitimately have
        real data under those names.
        """
        pending = self.data.local.legacy_region_rows()
        if not pending:
            messagebox.showinfo(
                APP_NAME,
                "Nothing to migrate - your database uses no legacy region labels.")
            return

        preview = "\n".join(
            f"  #{row['id']}  {row['metric_name']}  {row['region']} "
            f"{row['quarter']} {row['year']}  = {row['value']}"
            for row in pending[:15])
        more = f"\n  ...and {len(pending) - 15} more" if len(pending) > 15 else ""

        if not messagebox.askyesno(
                f"{APP_NAME} - migrate region labels",
                f"{len(pending)} row(s) still use the old demo region names.\n\n"
                f"{preview}{more}\n\n"
                f"Rename them to the neutral placeholders "
                f"(Europe -> Region_A, North America -> Region_B)?\n\n"
                f"Only do this if these are demo rows. If they are YOUR real "
                f"figures, choose No and keep your own labels."):
            return

        changed, message = self.data.local.migrate_region_labels()
        self._refresh_vocabularies()
        self._apply_startup_messages()
        messagebox.showinfo(APP_NAME, message)
        self.set_status(message)

    def action_diagnostics(self) -> None:
        """Show exactly which optional pieces are present - the first thing to
        check when something behaves unexpectedly."""
        lines = [
            f"{APP_NAME} v{__version__}",
            "",
            "NLP",
            f"  Extractor      : {self.engine.backend_name}",
            f"  Note           : {self.engine.backend_note or 'none'}",
            "",
            "Data sources",
            f"  {self.data.backend_status()}",
            f"  Local SQLite   : {self.data.local.path}",
            f"  Metrics known  : {', '.join(self.data.local.distinct_metrics()) or '(none)'}",
            f"  Latest period  : {self.data.latest_period()}",
            "",
            "Optional database drivers",
            self.data.driver_report(),
            "",
            "Regions (learned, not hardcoded)",
            f"  {self.regions.describe()}",
            "",
            "Derived metrics",
            f"  Active         : {', '.join(self.formula_engine.all_names()) or '(none)'}",
            f"  Rejected       : {len(self.formula_engine.errors)}",
            "",
            "Smart suggest",
            f"  Matcher        : {self.suggester.backend_name}",
            f"  Alias store    : "
            f"{'ready' if self.alias_store.available else 'unavailable'}",
            f"  Learned aliases: {len(self.alias_store.all_aliases())}",
            f"  Candidates     : {len(self.suggester.candidates)}",
            "",
            "Number formats",
            "  Supported      : US/UK, European, Swiss, Indian, space-separated",
            f"  Last document  : {self.engine.number_profile.describe()}",
            "",
            "Validation rules",
            f"  Enabled        : {self.verifier.validation.enabled}",
            f"  Ranges defined : {len(self.verifier.validation.ranges)}",
            "",
            "Other optional packages",
            f"  tkinterdnd2 (drag and drop): "
            f"{'installed' if DND_AVAILABLE else 'not installed'}",
            "",
            "Configuration",
            f"  File           : {self.config.path}",
            f"  Tolerance      : {self.verifier.tolerance}%",
        ]
        from .dialogs import show_text_window
        show_text_window(self.root, "Backend Diagnostics", "\n".join(lines), height=26)

    # ==================================================================
    # LIFECYCLE
    # ==================================================================
    def _on_close(self) -> None:
        if self._busy():
            if not messagebox.askyesno(
                    APP_NAME, "A verification job is still running. Quit anyway?"):
                return
            self.cancel_flag.set()

        # Release any worker blocked on a suggestion dialog, then stop the
        # queue pump before tearing the window down.
        self._suggest_stopped = True
        self._suggest_event.set()
        if getattr(self, "_after_id", None):
            try:
                self.root.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


# ---------------------------------------------------------------------------
# small formatting helpers shared by the log and the detail pane
# ---------------------------------------------------------------------------
def _num(value: Optional[float]) -> str:
    if value is None:
        return "-"
    if abs(value) >= 1000:
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{value:,.4f}".rstrip("0").rstrip(".") or "0"


def _pct(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.3f}%"
