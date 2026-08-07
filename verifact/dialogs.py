"""
dialogs.py
==========

Small secondary windows used by the main GUI:

  * :class:`DatabaseBrowser` - read-only view of the local SQLite truth table,
    so a user can see exactly what they are being measured against.
  * :func:`ask_new_value`    - the prompt shown by the right-click action
    "Correct the Claim in Database".
  * :func:`show_about` / :func:`show_help` - static information windows.

Keeping these out of ``gui.py`` keeps each file comfortably readable.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from typing import Optional

from . import APP_NAME, __version__


class DatabaseBrowser(tk.Toplevel):
    """A modal window listing every row of the local ``financial_metrics``."""

    def __init__(self, parent: tk.Misc, local_db) -> None:
        super().__init__(parent)
        self.title(f"{APP_NAME} - Local Truth Database")
        self.geometry("760x460")
        self.transient(parent)
        self.local_db = local_db

        header = ttk.Label(
            self,
            text=f"Table: {local_db.table}    File: {local_db.path}",
            padding=(10, 8),
        )
        header.pack(fill="x")

        columns = ("id", "metric_name", "region", "quarter", "year", "value")
        self.tree = ttk.Treeview(self, columns=columns, show="headings",
                                 selectmode="browse")
        widths = {"id": 50, "metric_name": 180, "region": 140,
                  "quarter": 80, "year": 80, "value": 160}
        for column in columns:
            self.tree.heading(column, text=column)
            self.tree.column(column, width=widths[column],
                             anchor="e" if column in ("id", "year", "value") else "w")

        scroll = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=(0, 10))
        scroll.pack(side="left", fill="y", padx=(0, 10), pady=(0, 10))

        self.refresh()

        buttons = ttk.Frame(self)
        buttons.pack(side="bottom", fill="x")
        ttk.Button(buttons, text="Refresh", command=self.refresh).pack(
            side="right", padx=6, pady=6)
        ttk.Button(buttons, text="Close", command=self.destroy).pack(
            side="right", pady=6)

    def refresh(self) -> None:
        """Re-read the table (values change when the user corrects a claim)."""
        self.tree.delete(*self.tree.get_children())
        for row in self.local_db.all_rows():
            self.tree.insert("", "end", values=(
                row["id"], row["metric_name"], row["region"],
                row["quarter"], row["year"], row["value"],
            ))


def ask_new_value(parent: tk.Misc, metric: str, current: Optional[float],
                  suggested: Optional[float]) -> Optional[float]:
    """Prompt for a replacement truth value.

    Returns the new float, or ``None`` if the user cancelled or typed junk.
    """
    prompt = (
        f"Update the stored value for '{metric}'.\n\n"
        f"Current database value: {current}\n"
        f"Claimed value from the document: {suggested}\n\n"
        f"Enter the correct value:"
    )
    answer = simpledialog.askstring(
        "Correct the Claim in Database", prompt, parent=parent,
        initialvalue=str(suggested if suggested is not None else current or ""),
    )
    if answer is None:
        return None
    try:
        return float(str(answer).replace(",", "").strip())
    except ValueError:
        messagebox.showerror("Invalid number",
                             f"'{answer}' is not a valid number.", parent=parent)
        return None


ABOUT_TEXT = f"""{APP_NAME}  v{__version__}

An offline claim-verification workbench.

Paste text from ChatGPT, an email or a report - or drop in a batch of
.txt / .log / .csv / .xlsx / .json files - and Veri-Fact will:

  1. Extract every number and its surrounding context.
  2. Work out which business metric and time period it refers to.
  3. Look the true value up in your database.
  4. Mark it GREEN (within 1%), RED (wrong) or AMBER (uncheckable).

No web server, no browser, no telemetry. Everything runs locally.
"""

HELP_TEXT = """HOW TO USE VERI-FACT DESKTOP

TAB 1 - PASTE TEXT
  1. Paste or type text containing numeric claims.
  2. Press "Verify Text" (or Ctrl+Enter).
  3. Read the colour-coded log underneath.

TAB 2 - BATCH FILES
  1. Click "Import Files" and multi-select your files,
     or drag them straight onto the file list.
  2. Press "Verify All Files".
  3. Click any row in the table to read its full original context.
  4. Right-click a row for:
        - Correct the Claim in Database
        - Copy Source Query
        - Copy Row / Copy Context

EXPORTING
  File > Export Results, then choose .csv, .xlsx (colour coded) or .json.

VERDICTS
  GREEN  PASS     within the tolerance (default 1%).
  RED    FAIL     the database disagrees; the true value is shown.
  AMBER  WARNING  unknown metric, region or period - nothing to compare.

CONNECTING YOUR OWN DATABASE
  Close the app, open config.json in Notepad, set
      "external_database": { "enabled": true, ... }
  fill in type / host / port / database / user / password, then reopen.
  Supported: postgresql (psycopg2), mysql (pymysql), mssql (pyodbc).
  If the external lookup misses, Veri-Fact falls back to local SQLite.

TEACHING IT NEW WORDS
  Open verifact/nlp_engine.py and add entries to METRIC_KEYWORD_MAP,
  for example:   "arr": "annual_recurring_revenue"
  Matching is substring based, so "our ARR" and "arr_total" both hit.
"""


def show_text_window(parent: tk.Misc, title: str, body: str,
                     width: int = 78, height: int = 26) -> None:
    """Generic scrollable, read-only text popup used by About and Help."""
    window = tk.Toplevel(parent)
    window.title(title)
    window.transient(parent)

    frame = ttk.Frame(window, padding=10)
    frame.pack(fill="both", expand=True)

    text = tk.Text(frame, wrap="word", width=width, height=height,
                   font=("Consolas", 10), relief="flat",
                   background="#FBFBFB")
    scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=scroll.set)
    text.insert("1.0", body)
    text.configure(state="disabled")

    text.pack(side="left", fill="both", expand=True)
    scroll.pack(side="left", fill="y")

    ttk.Button(window, text="Close", command=window.destroy).pack(pady=(0, 10))


def show_about(parent: tk.Misc) -> None:
    show_text_window(parent, f"About {APP_NAME}", ABOUT_TEXT, height=18)


def show_help(parent: tk.Misc) -> None:
    show_text_window(parent, f"{APP_NAME} Help", HELP_TEXT, height=32)


# ===========================================================================
# SMART-SUGGEST DIALOG
# ===========================================================================
class SuggestionDialog(tk.Toplevel):
    """Asks 'Did you mean X?' with Yes / No / Ignore.

    Returned via :attr:`result`, one of:
        "yes"    - accept the suggestion and remember it
        "no"     - reject THIS pairing but keep offering others later
        "ignore" - never ask about this text again
        "stop"   - stop prompting for the rest of this batch

    The dialog is modal (``grab_set`` + ``wait_window``) because it must be
    answered before verification of the current claim can finish. It is only
    ever shown from the MAIN thread - the worker posts a request and waits on
    an Event, which keeps Tk's single-threaded rule intact.
    """

    def __init__(self, parent: tk.Misc, suggestion, alternatives=None) -> None:
        super().__init__(parent)
        self.title("Unknown metric - did you mean...?")
        self.transient(parent)
        self.resizable(False, False)
        self.result: Optional[str] = None
        self.chosen_metric: str = suggestion.suggested_metric
        self._alternatives = list(alternatives or [])

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Unrecognised metric name",
                  font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(frame, text=f"\nFound in the document:  '{suggestion.source_text}'",
                  wraplength=460, justify="left").pack(anchor="w")

        ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=10)

        ttk.Label(frame, text="Did you mean:",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")

        # A combobox lets the user pick a different candidate than our best
        # guess - often they know better than the score does.
        options = [suggestion.suggested_metric] + [
            s.suggested_metric for s in self._alternatives
            if s.suggested_metric != suggestion.suggested_metric
        ]
        self.choice = tk.StringVar(value=suggestion.suggested_metric)
        combo = ttk.Combobox(frame, textvariable=self.choice, values=options,
                             state="readonly", width=42)
        combo.pack(anchor="w", pady=(4, 8))

        ttk.Label(frame, text=f"Confidence: {suggestion.score:.0%}\n"
                              f"Why: {suggestion.reason}",
                  wraplength=460, justify="left",
                  foreground="#555555").pack(anchor="w")

        ttk.Label(frame,
                  text="\nYour answer is remembered for future sessions.",
                  foreground="#777777").pack(anchor="w")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="Yes",
                   command=lambda: self._finish("yes")).pack(side="left")
        ttk.Button(buttons, text="No",
                   command=lambda: self._finish("no")).pack(side="left", padx=6)
        ttk.Button(buttons, text="Ignore",
                   command=lambda: self._finish("ignore")).pack(side="left")
        ttk.Button(buttons, text="Stop asking",
                   command=lambda: self._finish("stop")).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", lambda: self._finish("ignore"))
        self.bind("<Escape>", lambda e: self._finish("ignore"))
        self.bind("<Return>", lambda e: self._finish("yes"))

        _centre_on_parent(self, parent)
        self.grab_set()
        self.wait_window(self)

    def _finish(self, verdict: str) -> None:
        self.result = verdict
        self.chosen_metric = self.choice.get() or self.chosen_metric
        self.destroy()


class AliasManager(tk.Toplevel):
    """Browse and delete the metric corrections the user has taught the app."""

    def __init__(self, parent: tk.Misc, store) -> None:
        super().__init__(parent)
        self.title(f"{APP_NAME} - Learned Metric Corrections")
        self.geometry("720x400")
        self.transient(parent)
        self.store = store

        ttk.Label(self, padding=(10, 8),
                  text="Corrections you have taught Veri-Fact. "
                       "Delete a row to be asked about it again.").pack(fill="x")

        columns = ("id", "source_text", "metric_name", "verdict", "hit_count")
        self.tree = ttk.Treeview(self, columns=columns, show="headings")
        widths = {"id": 45, "source_text": 240, "metric_name": 200,
                  "verdict": 90, "hit_count": 70}
        titles = {"id": "ID", "source_text": "Text in document",
                  "metric_name": "Mapped to", "verdict": "Answer",
                  "hit_count": "Used"}
        for column in columns:
            self.tree.heading(column, text=titles[column])
            self.tree.column(column, width=widths[column],
                             anchor="e" if column in ("id", "hit_count") else "w")
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=(0, 10))
        scroll.pack(side="left", fill="y", padx=(0, 10), pady=(0, 10))

        buttons = ttk.Frame(self)
        buttons.pack(side="bottom", fill="x")
        ttk.Button(buttons, text="Delete Selected",
                   command=self.delete_selected).pack(side="left", padx=6, pady=6)
        ttk.Button(buttons, text="Close", command=self.destroy).pack(
            side="right", padx=6, pady=6)

        self.refresh()

    def refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for row in self.store.all_aliases():
            self.tree.insert("", "end", values=(
                row.get("id"), row.get("source_text"), row.get("metric_name"),
                row.get("verdict"), row.get("hit_count", 0)))

    def delete_selected(self) -> None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo(APP_NAME, "Select a row first.", parent=self)
            return
        row_id = self.tree.item(selection[0], "values")[0]
        if self.store.forget(int(row_id)):
            self.refresh()
        else:
            messagebox.showerror(APP_NAME, "Could not delete that row.", parent=self)


def _centre_on_parent(window: tk.Toplevel, parent: tk.Misc) -> None:
    """Position a dialog over the middle of its parent window."""
    window.update_idletasks()
    try:
        parent_x = parent.winfo_rootx()
        parent_y = parent.winfo_rooty()
        parent_w = parent.winfo_width()
        parent_h = parent.winfo_height()
        width = window.winfo_width()
        height = window.winfo_height()
        x = parent_x + max(0, (parent_w - width) // 2)
        y = parent_y + max(0, (parent_h - height) // 3)
        window.geometry(f"+{x}+{y}")
    except tk.TclError:
        pass
