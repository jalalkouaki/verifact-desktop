"""
config_manager.py
=================

WHAT THIS FILE DOES
-------------------
Veri-Fact can check claims against TWO data sources:

  1. A local SQLite file (``verifact.db``) that ships with the app and is
     auto-created + seeded on first launch. This always works offline.
  2. An OPTIONAL *external* enterprise database (PostgreSQL, MySQL or Microsoft
     SQL Server) that YOU describe in a plain-text ``config.json`` file sitting
     next to ``main.py``.

This module is responsible ONLY for reading/writing/validating that JSON file.
It never touches a database itself - that is ``database.py``'s job.

HOW A NON-EXPERT CHANGES THINGS
-------------------------------
Open ``config.json`` in Notepad. To point Veri-Fact at your own warehouse set
``external_database.enabled`` to ``true`` and fill in the fields. Example for
PostgreSQL::

    "external_database": {
        "enabled": true,
        "type": "postgresql",
        "host": "10.0.0.14",
        "port": 5432,
        "database": "analytics",
        "user": "readonly_bob",
        "password": "hunter2",
        "table": "financial_metrics",
        "columns": {
            "metric_name": "metric_name",
            "region":      "region",
            "quarter":     "quarter",
            "year":        "year",
            "value":       "value"
        }
    }

The ``columns`` block is a *rename map*. The key on the LEFT is the logical
name Veri-Fact uses internally; the value on the RIGHT is whatever the column
is actually called in YOUR table. So if your warehouse calls revenue amounts
``amt_usd``, you write ``"value": "amt_usd"`` and nothing else changes.

You may also supply a raw SQLAlchemy-style DSN instead of the individual
fields via ``"connection_string"``; if present it wins over host/port/etc.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List


# ---------------------------------------------------------------------------
# The default configuration. This exact structure is written to disk the very
# first time the application starts (so the user has something to edit).
# ---------------------------------------------------------------------------
DEFAULT_CONFIG: Dict[str, Any] = {
    "_README": (
        "Veri-Fact Desktop configuration. Set external_database.enabled to true "
        "and fill in your credentials to verify claims against a real data "
        "warehouse. Leave it false to use only the bundled local SQLite file."
    ),
    "local_database": {
        # Path is relative to the folder containing main.py unless absolute.
        "path": "verifact.db",
        "table": "financial_metrics",
        # Re-seed the demo rows if the table comes up empty.
        "seed_on_first_run": True,
    },
    "external_database": {
        "enabled": False,
        # One of: postgresql | mysql | mssql
        "type": "postgresql",
        # EITHER provide a full DSN here (takes priority) ...
        "connection_string": "",
        # ... OR fill in these discrete fields.
        "host": "localhost",
        "port": 5432,
        "database": "analytics",
        "user": "",
        "password": "",
        # Only used by mssql (pyodbc). Change if you have a different driver.
        "odbc_driver": "ODBC Driver 17 for SQL Server",
        "table": "financial_metrics",
        # Map Veri-Fact's logical column names -> your real column names.
        "columns": {
            "metric_name": "metric_name",
            "region": "region",
            "quarter": "quarter",
            "year": "year",
            "value": "value",
        },
        # Seconds before giving up on a slow warehouse.
        "timeout_seconds": 8,
    },
    "verification": {
        # A claim is GREEN when |claimed - actual| / actual * 100 < this number.
        "tolerance_percent": 1.0,
        # When a claim mentions no timeframe we fall back to the newest period
        # present in the database.
        "default_to_latest_period": True,
        # Used by the "last quarter" style relative parser so the demo data
        # (Q3 2024) resolves nicely. Set to 0 to use the real system clock.
        "demo_reference_year": 2024,
        "demo_reference_quarter": 4,
    },

    # -------------------------------------------------------------------
    # REGIONS - learned from your data, not hardcoded.
    # -------------------------------------------------------------------
    # Veri-Fact runs "SELECT DISTINCT region" against your database at startup
    # and recognises whatever labels it finds. No geography is assumed.
    "regions": {
        # Read the live vocabulary from the database. Turn off only if your
        # region column is huge or slow.
        "load_from_database": True,
        # Used when the database is unreachable or the table is empty.
        "fallback": ["Global", "Region_A", "Region_B"],
        # Optional prose synonyms: canonical label -> words a writer may use.
        # Example: "Region_A": ["emea", "europe", "european"]
        "synonyms": {},
        # Ignore region tokens shorter than this (stops "AU" firing inside
        # "August"). Raise it if you get false positives.
        "min_token_length": 2,
    },

    # -------------------------------------------------------------------
    # DERIVED METRICS - values calculated from other metrics.
    # -------------------------------------------------------------------
    # When a claim names one of these keys, Veri-Fact evaluates the expression
    # from constituent database values instead of reading one row.
    # Only numbers, metric names, + - * / // % ** and the functions
    # min/max/abs/round/sum/avg/sqrt are permitted - expressions are parsed and
    # whitelisted, never eval()'d.
    "derived_metrics": {
        "total_revenue": "region_a_revenue + region_b_revenue",
        "arpu": "revenue / active_users",
    },

    # -------------------------------------------------------------------
    # VALIDATION RULES - sanity checks applied before/alongside DB lookup.
    # -------------------------------------------------------------------
    "validation_rules": {
        # Master switch for everything in this block.
        "enabled": True,
        # Values outside a metric's range are flagged even if the database
        # agrees - useful for catching unit mix-ups (a 3800% churn rate).
        "ranges": {
            "churn_rate": {"min": 0.0, "max": 100.0, "unit": "percent"},
            "gross_margin": {"min": -100.0, "max": 100.0, "unit": "percent"},
            "conversion_rate": {"min": 0.0, "max": 100.0, "unit": "percent"},
            "growth_rate": {"min": -100.0, "max": 1000.0, "unit": "percent"},
        },
        # Metrics that can never legitimately be negative.
        "non_negative": ["active_users", "customer_count", "revenue",
                         "total_sales", "visits", "clicks", "impressions"],
        # Any metric whose name matches one of these is treated as a
        # percentage for range checking, even without an explicit rule.
        "percent_metric_suffixes": ["_rate", "_pct", "_percent", "margin"],
        # How a range breach is reported: "warning" (AMBER) or "fail" (RED).
        "on_violation": "warning",
    },

    # -------------------------------------------------------------------
    # SMART SUGGEST - fuzzy matching for unrecognised metric names.
    # -------------------------------------------------------------------
    "suggestions": {
        "enabled": True,
        # Minimum match confidence (0-1) before the user is prompted.
        "threshold": 0.55,
        # Ask interactively during a run. Turn off for unattended batches -
        # unmatched metrics then simply stay AMBER.
        "prompt_user": True,
        # Remember answers in the metric_aliases table across sessions.
        "remember_choices": True,
        # Cap the prompts in one batch so a bad file cannot spam the user.
        "max_prompts_per_run": 12,
    },

    # -------------------------------------------------------------------
    # NUMBER FORMATS - global by default.
    # -------------------------------------------------------------------
    "number_formats": {
        # Detect the convention (US / European / Swiss / Indian / spaced)
        # per document from its own contents.
        "auto_detect": True,
        # Force one convention instead: us | eu | swiss | indian | space
        "force_profile": "",
        # Render results using the source document's convention.
        "display_using_source_profile": False,
    },

    "ui": {
        "window_width": 1180,
        "window_height": 760,
        "theme": "clam",
    },
}


class ConfigManager:
    """Loads, validates and saves ``config.json``.

    The class is deliberately forgiving: any missing key falls back to the
    matching entry in :data:`DEFAULT_CONFIG`, so a half-edited config file can
    never crash the application.
    """

    def __init__(self, base_dir: str, filename: str = "config.json") -> None:
        self.base_dir = base_dir
        self.path = os.path.join(base_dir, filename)
        self.data: Dict[str, Any] = {}
        self.load_errors: list[str] = []
        self.load()

    # -- loading ----------------------------------------------------------
    def load(self) -> Dict[str, Any]:
        """Read config.json from disk, creating it with defaults if absent."""
        if not os.path.exists(self.path):
            self.data = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
            self.save()
            return self.data

        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                on_disk = json.load(handle)
            if not isinstance(on_disk, dict):
                raise ValueError("config.json must contain a JSON object")
        except Exception as exc:  # corrupted / unreadable -> use defaults
            self.load_errors.append(f"Could not read config.json ({exc}). Using defaults.")
            on_disk = {}

        self.data = _deep_merge(json.loads(json.dumps(DEFAULT_CONFIG)), on_disk)
        return self.data

    def save(self) -> None:
        """Write the in-memory configuration back to disk (pretty printed)."""
        try:
            with open(self.path, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, indent=4)
        except OSError as exc:
            self.load_errors.append(f"Could not write config.json ({exc}).")

    # -- convenience accessors -------------------------------------------
    def get(self, *keys: str, default: Any = None) -> Any:
        """Safely walk nested keys: ``cfg.get('external_database', 'type')``."""
        node: Any = self.data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    @property
    def local_db_path(self) -> str:
        """Absolute path to the SQLite file."""
        raw = self.get("local_database", "path", default="verifact.db")
        return raw if os.path.isabs(raw) else os.path.join(self.base_dir, raw)

    @property
    def tolerance_percent(self) -> float:
        try:
            return float(self.get("verification", "tolerance_percent", default=1.0))
        except (TypeError, ValueError):
            return 1.0

    @property
    def external_enabled(self) -> bool:
        return bool(self.get("external_database", "enabled", default=False))

    # -- new-feature accessors --------------------------------------------
    # Each one is defensive: a user who types a string where a list belongs
    # gets the default back rather than a crash.
    @property
    def region_fallback(self) -> List[str]:
        """Region labels to use when the database yields none."""
        value = self.get("regions", "fallback", default=[])
        if not isinstance(value, list):
            return []
        return [str(v).strip() for v in value if str(v).strip()]

    @property
    def region_synonyms(self) -> Dict[str, Any]:
        """Canonical label -> list of prose synonyms."""
        value = self.get("regions", "synonyms", default={})
        return value if isinstance(value, dict) else {}

    @property
    def load_regions_from_db(self) -> bool:
        return bool(self.get("regions", "load_from_database", default=True))

    @property
    def region_min_token_length(self) -> int:
        try:
            return max(1, int(self.get("regions", "min_token_length", default=2)))
        except (TypeError, ValueError):
            return 2

    @property
    def derived_metrics(self) -> Dict[str, str]:
        """Formula definitions, filtered to string values only."""
        value = self.get("derived_metrics", default={})
        if not isinstance(value, dict):
            return {}
        return {str(k): str(v) for k, v in value.items()
                if isinstance(v, str) and str(v).strip()}

    @property
    def validation_rules(self) -> Dict[str, Any]:
        value = self.get("validation_rules", default={})
        return value if isinstance(value, dict) else {}

    @property
    def validation_enabled(self) -> bool:
        return bool(self.validation_rules.get("enabled", True))

    @property
    def suggestions_enabled(self) -> bool:
        return bool(self.get("suggestions", "enabled", default=True))

    @property
    def suggestion_threshold(self) -> float:
        try:
            return float(self.get("suggestions", "threshold", default=0.55))
        except (TypeError, ValueError):
            return 0.55

    @property
    def suggestion_prompt(self) -> bool:
        return bool(self.get("suggestions", "prompt_user", default=True))

    @property
    def max_prompts_per_run(self) -> int:
        try:
            return max(0, int(self.get("suggestions", "max_prompts_per_run",
                                       default=12)))
        except (TypeError, ValueError):
            return 12

    @property
    def forced_number_profile(self) -> str:
        raw = self.get("number_formats", "force_profile", default="")
        return str(raw or "").strip().lower()

    @property
    def display_using_source_profile(self) -> bool:
        return bool(self.get("number_formats", "display_using_source_profile",
                             default=False))


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively overlay ``override`` on top of ``base`` and return ``base``.

    Nested dictionaries are merged key-by-key rather than replaced wholesale,
    which is what lets a user delete half of config.json and still boot.
    """
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base
