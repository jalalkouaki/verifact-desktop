"""
database.py
===========

WHAT THIS FILE DOES
-------------------
This is the "source of truth" layer. It answers one question:

    "What is the REAL value of metric X, for region R, in quarter Q of year Y?"

It answers it by consulting up to two places, in this exact priority order:

    1. The EXTERNAL enterprise database (PostgreSQL / MySQL / SQL Server), but
       only if the user enabled it in config.json AND the matching driver
       package is installed.
    2. The LOCAL SQLite file ``verifact.db``, which is auto-created and seeded
       with demo rows on first launch.

If the external lookup misses (no matching row) we transparently fall back to
SQLite. Every answer carries a ``source`` string so the GUI can show the user
exactly where the number came from - that is the whole point of an audit tool.

DRIVER IMPORTS ARE OPTIONAL
---------------------------
psycopg2 / pymysql / pyodbc are imported lazily inside a try/except. If they
are missing the application still runs perfectly with SQLite only; we just
record a friendly message in ``ExternalDatabase.last_error``.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Optional third-party drivers. Never let a missing package kill the app.
# ---------------------------------------------------------------------------
DRIVER_AVAILABILITY: Dict[str, bool] = {}

try:  # PostgreSQL
    import psycopg2  # type: ignore
    DRIVER_AVAILABILITY["postgresql"] = True
except Exception:  # pragma: no cover - environment dependent
    psycopg2 = None  # type: ignore
    DRIVER_AVAILABILITY["postgresql"] = False

try:  # MySQL / MariaDB
    import pymysql  # type: ignore
    DRIVER_AVAILABILITY["mysql"] = True
except Exception:  # pragma: no cover
    pymysql = None  # type: ignore
    DRIVER_AVAILABILITY["mysql"] = False

try:  # Microsoft SQL Server
    import pyodbc  # type: ignore
    DRIVER_AVAILABILITY["mssql"] = True
except Exception:  # pragma: no cover
    pyodbc = None  # type: ignore
    DRIVER_AVAILABILITY["mssql"] = False


# ---------------------------------------------------------------------------
# The demo seed data required by the specification.
# ---------------------------------------------------------------------------
# NOTE ON REGION NAMES
# --------------------
# The demo data uses NEUTRAL placeholders - Region_A, Region_B, Global - rather
# than real-world geography. Two reasons:
#   1. Veri-Fact learns its region vocabulary from whatever is in YOUR table
#      (see RegionRegistry in nlp_engine.py). Shipping "Europe"/"North America"
#      implied the tool cared about those specific places. It does not.
#   2. Placeholders make it obvious at a glance that these rows are sample
#      data, not somebody's real figures.
# Replace them with your own labels - sales territories, business units, store
# IDs, legal entities - and everything adapts automatically.
SEED_ROWS: List[Tuple[str, str, str, int, float]] = [
    ("active_users", "Region_A", "Q3", 2024, 984.5),
    ("active_users", "Region_B", "Q3", 2024, 2100.0),
    ("revenue", "Global", "Q3", 2024, 4800000.0),
    ("churn_rate", "Global", "Q3", 2024, 2.3),
]

# A few extra rows make the demo more interesting (multiple quarters so the
# "last quarter" / "Q2" parsers have something to find). Delete freely.
EXTRA_SEED_ROWS: List[Tuple[str, str, str, int, float]] = [
    ("active_users", "Global", "Q2", 2024, 2850.0),
    ("revenue", "Global", "Q2", 2024, 4350000.0),
    ("revenue", "Region_A", "Q3", 2024, 1750000.0),
    ("revenue", "Region_B", "Q3", 2024, 3050000.0),
    ("churn_rate", "Region_A", "Q3", 2024, 1.8),
    ("total_sales", "Global", "Q3", 2024, 5250000.0),
    ("customer_count", "Global", "Q3", 2024, 18400.0),
    ("profit", "Global", "Q3", 2024, 960000.0),
    ("gross_margin", "Global", "Q3", 2024, 62.5),
    ("growth_rate", "Global", "Q3", 2024, 11.4),
    ("conversion_rate", "Global", "Q3", 2024, 3.7),
    # Constituents for the demo 'total_revenue' formula in config.json.
    ("region_a_revenue", "Global", "Q3", 2024, 1750000.0),
    ("region_b_revenue", "Global", "Q3", 2024, 3050000.0),
]

# ---------------------------------------------------------------------------
# SCHEMA MIGRATION
# ---------------------------------------------------------------------------
# Bump this when the shape of the database changes. ``LocalDatabase.initialise``
# compares it against the value stored in the ``schema_meta`` table and runs
# the matching migration steps. Users upgrading from an older Veri-Fact keep
# their data - nothing is ever dropped.
SCHEMA_VERSION = 2

# Region labels that v1 shipped as demo data. On upgrade we offer to rename
# them to the neutral placeholders, but ONLY if the row is untouched demo data
# (see LocalDatabase.migrate_region_labels).
LEGACY_REGION_RENAMES: Dict[str, str] = {
    "Europe": "Region_A",
    "North America": "Region_B",
}


@dataclass
class LookupResult:
    """The outcome of asking the data layer for a truth value."""

    found: bool
    value: Optional[float] = None
    source: str = ""                 # e.g. "SQLite: financial_metrics#3"
    row: Dict[str, Any] = field(default_factory=dict)
    query: str = ""                  # exact SQL, for "Copy Source Query"
    params: Tuple[Any, ...] = ()
    note: str = ""                   # human explanation when not found


# ===========================================================================
# LOCAL SQLITE
# ===========================================================================
class LocalDatabase:
    """Thin wrapper around the bundled ``verifact.db`` SQLite file.

    SQLite connections are not safe to share across threads, and the GUI runs
    verification on a worker thread, so we simply open a short-lived connection
    per operation and guard writes with a lock. For a desktop app handling a
    few thousand rows this is more than fast enough and removes a whole class
    of threading bugs.
    """

    def __init__(self, path: str, table: str = "financial_metrics") -> None:
        self.path = path
        self.table = table
        self._lock = threading.Lock()
        # Populated by initialise(); the GUI reads these to decide whether to
        # offer the region-label migration on startup.
        self.schema_version_before: int = SCHEMA_VERSION
        self.was_fresh_install: bool = False
        self.pending_migration: bool = False
        self.initialise()

    # -- schema ----------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def initialise(self) -> None:
        """Create the table if needed, seed it when empty, then migrate.

        The schema is exactly the one required by the brief:
            id, metric_name, region, quarter, year, value

        Plus a tiny ``schema_meta`` table holding the schema version, which is
        what makes safe upgrades possible for users who already have data.
        """
        with self._lock, self._connect() as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.table} (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    metric_name TEXT    NOT NULL,
                    region      TEXT    NOT NULL,
                    quarter     TEXT    NOT NULL,
                    year        INTEGER NOT NULL,
                    value       REAL    NOT NULL
                )
                """
            )
            # Helpful index: nearly every query filters on these three columns.
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self.table}_lookup "
                f"ON {self.table} (metric_name, year, quarter)"
            )
            # Version marker used by the migration logic below.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            existing_version = int(row["value"]) if row else 0

            count = conn.execute(f"SELECT COUNT(*) FROM {self.table}").fetchone()[0]
            fresh_install = count == 0

            if fresh_install:
                conn.executemany(
                    f"INSERT INTO {self.table} "
                    f"(metric_name, region, quarter, year, value) VALUES (?,?,?,?,?)",
                    SEED_ROWS + EXTRA_SEED_ROWS,
                )

            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),))

        self.schema_version_before = existing_version
        self.was_fresh_install = fresh_install
        # A brand-new file is already at the current version; only an existing
        # v0/v1 database needs the data migration.
        if not fresh_install and existing_version < SCHEMA_VERSION:
            self.pending_migration = True
        else:
            self.pending_migration = False

    # -- migration ---------------------------------------------------------
    def legacy_region_rows(self) -> List[Dict[str, Any]]:
        """Rows still using the old hardcoded demo region names.

        Returned so the GUI can SHOW the user exactly what would change before
        anything is modified. We never rewrite a user's data silently.
        """
        if not LEGACY_REGION_RENAMES:
            return []
        placeholders = ",".join("?" for _ in LEGACY_REGION_RENAMES)
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    f"SELECT id, metric_name, region, quarter, year, value "
                    f"FROM {self.table} WHERE region IN ({placeholders})",
                    tuple(LEGACY_REGION_RENAMES)).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error:
            return []

    def migrate_region_labels(self) -> Tuple[int, str]:
        """Rename legacy demo regions to the neutral placeholders.

        Returns ``(rows_changed, message)``. This is OPT-IN - the GUI asks
        first - because renaming regions in a database the user has populated
        themselves would be destructive.
        """
        pending = self.legacy_region_rows()
        if not pending:
            return 0, "Nothing to migrate - no legacy region labels found."

        changed = 0
        try:
            with self._lock, self._connect() as conn:
                for old, new in LEGACY_REGION_RENAMES.items():
                    cursor = conn.execute(
                        f"UPDATE {self.table} SET region = ? WHERE region = ?",
                        (new, old))
                    changed += cursor.rowcount or 0
        except sqlite3.Error as exc:
            return 0, f"Migration failed: {exc}"

        return changed, (f"Renamed {changed} row(s) to neutral region labels "
                         f"({', '.join(f'{o} -> {n}' for o, n in LEGACY_REGION_RENAMES.items())}).")

    # -- region vocabulary --------------------------------------------------
    def distinct_regions(self) -> List[str]:
        """``SELECT DISTINCT region`` - the live region vocabulary.

        This is what replaces the old hardcoded REGION_KEYWORDS list. Whatever
        labels exist in your data become the labels Veri-Fact understands.
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    f"SELECT DISTINCT region FROM {self.table} "
                    f"WHERE region IS NOT NULL AND TRIM(region) <> '' "
                    f"ORDER BY region").fetchall()
            return [str(r[0]).strip() for r in rows if str(r[0]).strip()]
        except sqlite3.Error:
            return []

    # -- reads -----------------------------------------------------------
    def latest_period(self) -> Optional[Tuple[str, int]]:
        """Return ``(quarter, year)`` of the newest data we hold.

        Used when a claim mentions no timeframe at all: "we had 984.5k users"
        is assumed to talk about the most recent period on record.
        """
        try:
            with self._connect() as conn:
                row = conn.execute(
                    f"SELECT quarter, year FROM {self.table} "
                    f"ORDER BY year DESC, quarter DESC LIMIT 1"
                ).fetchone()
            return (row["quarter"], int(row["year"])) if row else None
        except sqlite3.Error:
            return None

    def lookup(
        self,
        metric: str,
        quarter: Optional[str] = None,
        year: Optional[int] = None,
        region: Optional[str] = None,
    ) -> LookupResult:
        """Find the truth value for a metric/period/region combination.

        Matching strategy, from strict to loose:
          * metric_name always matches case-insensitively and exactly.
          * quarter/year/region are only constrained when supplied.
          * If several rows qualify (e.g. no region given but Europe + NA both
            exist) we prefer a 'Global' row, otherwise we take the newest one
            and report the region we used so the user is never misled.
        """
        where = [f"LOWER({_q('metric_name')}) = LOWER(?)"]
        params: List[Any] = [metric]

        if quarter:
            where.append(f"UPPER({_q('quarter')}) = UPPER(?)")
            params.append(quarter)
        if year:
            where.append(f"{_q('year')} = ?")
            params.append(int(year))
        if region:
            where.append(f"LOWER({_q('region')}) = LOWER(?)")
            params.append(region)

        sql = (
            f"SELECT id, metric_name, region, quarter, year, value "
            f"FROM {self.table} WHERE " + " AND ".join(where) +
            " ORDER BY CASE WHEN LOWER(region)='global' THEN 0 ELSE 1 END, "
            "year DESC, quarter DESC"
        )

        try:
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            return LookupResult(False, note=f"SQLite error: {exc}", query=sql,
                                params=tuple(params))

        if not rows:
            return LookupResult(
                False,
                note="No matching row in local SQLite.",
                query=sql,
                params=tuple(params),
            )

        best = dict(rows[0])
        return LookupResult(
            found=True,
            value=float(best["value"]),
            source=f"SQLite:{self.table}#{best['id']} "
                   f"({best['region']} {best['quarter']} {best['year']})",
            row=best,
            query=sql,
            params=tuple(params),
        )

    def distinct_metrics(self) -> List[str]:
        """All metric names we know about - shown in the Help > Data dialog."""
        try:
            with self._connect() as conn:
                return [r[0] for r in conn.execute(
                    f"SELECT DISTINCT metric_name FROM {self.table} ORDER BY 1")]
        except sqlite3.Error:
            return []

    def all_rows(self) -> List[Dict[str, Any]]:
        """Dump the whole table (used by the 'Browse Database' window)."""
        try:
            with self._connect() as conn:
                return [dict(r) for r in conn.execute(
                    f"SELECT id, metric_name, region, quarter, year, value "
                    f"FROM {self.table} ORDER BY metric_name, year, quarter")]
        except sqlite3.Error:
            return []

    # -- writes ----------------------------------------------------------
    def update_value(self, row_id: int, new_value: float) -> bool:
        """Used by the right-click 'Correct the Claim in Database' action."""
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    f"UPDATE {self.table} SET value = ? WHERE id = ?",
                    (float(new_value), int(row_id)),
                )
            return True
        except sqlite3.Error:
            return False

    def insert_row(self, metric: str, region: str, quarter: str,
                   year: int, value: float) -> Optional[int]:
        """Add a brand-new truth row (offered when a metric is missing)."""
        try:
            with self._lock, self._connect() as conn:
                cur = conn.execute(
                    f"INSERT INTO {self.table} "
                    f"(metric_name, region, quarter, year, value) VALUES (?,?,?,?,?)",
                    (metric, region, quarter, int(year), float(value)),
                )
                return cur.lastrowid
        except sqlite3.Error:
            return None


# ===========================================================================
# EXTERNAL ENTERPRISE DATABASE
# ===========================================================================
class ExternalDatabase:
    """Optional connector for PostgreSQL / MySQL / Microsoft SQL Server.

    Everything here is defensive: a warehouse that is down, slow, or missing a
    driver must never prevent the user from verifying against local SQLite.
    ``self.available`` tells the GUI whether to advertise the external source
    in the status bar.
    """

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg or {}
        self.kind = str(self.cfg.get("type", "postgresql")).lower().strip()
        self.table = self.cfg.get("table", "financial_metrics")
        self.columns: Dict[str, str] = dict(self.cfg.get("columns", {})) or {
            "metric_name": "metric_name",
            "region": "region",
            "quarter": "quarter",
            "year": "year",
            "value": "value",
        }
        self.enabled = bool(self.cfg.get("enabled", False))
        self.last_error: str = ""
        self.available: bool = False
        if self.enabled:
            self.available = self._probe()

    # -- connection -------------------------------------------------------
    def _probe(self) -> bool:
        """Try to connect once at startup so the status bar can be honest."""
        if not DRIVER_AVAILABILITY.get(self.kind, False):
            self.last_error = (
                f"Driver for '{self.kind}' is not installed. "
                f"Run: pip install " + {
                    "postgresql": "psycopg2-binary",
                    "mysql": "pymysql",
                    "mssql": "pyodbc",
                }.get(self.kind, self.kind)
            )
            return False
        try:
            conn = self._connect()
            conn.close()
            self.last_error = ""
            return True
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False

    def _connect(self):
        """Open a DB-API connection using whichever driver the config names.

        A raw ``connection_string`` always wins if the user supplied one - some
        shops mandate exotic options we cannot model with discrete fields.
        """
        dsn = str(self.cfg.get("connection_string", "") or "").strip()
        timeout = int(self.cfg.get("timeout_seconds", 8) or 8)

        if self.kind == "postgresql":
            if psycopg2 is None:
                raise RuntimeError("psycopg2 not installed")
            if dsn:
                return psycopg2.connect(dsn, connect_timeout=timeout)
            return psycopg2.connect(
                host=self.cfg.get("host", "localhost"),
                port=int(self.cfg.get("port", 5432) or 5432),
                dbname=self.cfg.get("database", ""),
                user=self.cfg.get("user", ""),
                password=self.cfg.get("password", ""),
                connect_timeout=timeout,
            )

        if self.kind == "mysql":
            if pymysql is None:
                raise RuntimeError("pymysql not installed")
            return pymysql.connect(
                host=self.cfg.get("host", "localhost"),
                port=int(self.cfg.get("port", 3306) or 3306),
                database=self.cfg.get("database", ""),
                user=self.cfg.get("user", ""),
                password=self.cfg.get("password", ""),
                connect_timeout=timeout,
            )

        if self.kind == "mssql":
            if pyodbc is None:
                raise RuntimeError("pyodbc not installed")
            if dsn:
                return pyodbc.connect(dsn, timeout=timeout)
            conn_str = (
                f"DRIVER={{{self.cfg.get('odbc_driver', 'ODBC Driver 17 for SQL Server')}}};"
                f"SERVER={self.cfg.get('host', 'localhost')},"
                f"{self.cfg.get('port', 1433)};"
                f"DATABASE={self.cfg.get('database', '')};"
                f"UID={self.cfg.get('user', '')};"
                f"PWD={self.cfg.get('password', '')}"
            )
            return pyodbc.connect(conn_str, timeout=timeout)

        raise RuntimeError(f"Unsupported external database type: {self.kind!r}")

    # -- placeholder style -------------------------------------------------
    @property
    def _ph(self) -> str:
        """Parameter placeholder: psycopg2/pymysql use %s, pyodbc uses ?."""
        return "?" if self.kind == "mssql" else "%s"

    # -- reads -------------------------------------------------------------
    def lookup(
        self,
        metric: str,
        quarter: Optional[str] = None,
        year: Optional[int] = None,
        region: Optional[str] = None,
    ) -> LookupResult:
        """Same contract as :meth:`LocalDatabase.lookup` but over the network."""
        if not (self.enabled and self.available):
            return LookupResult(False, note="External database disabled/unavailable.")

        c = self.columns
        ph = self._ph
        where = [f"LOWER({c['metric_name']}) = LOWER({ph})"]
        params: List[Any] = [metric]

        if quarter:
            where.append(f"UPPER({c['quarter']}) = UPPER({ph})")
            params.append(quarter)
        if year:
            where.append(f"{c['year']} = {ph}")
            params.append(int(year))
        if region:
            where.append(f"LOWER({c['region']}) = LOWER({ph})")
            params.append(region)

        sql = (
            f"SELECT {c['metric_name']}, {c['region']}, {c['quarter']}, "
            f"{c['year']}, {c['value']} FROM {self.table} "
            f"WHERE " + " AND ".join(where) +
            f" ORDER BY {c['year']} DESC, {c['quarter']} DESC"
        )

        try:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute(sql, tuple(params))
                row = cur.fetchone()
                cur.close()
            finally:
                conn.close()
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return LookupResult(False, note=f"External query failed: {exc}",
                                query=sql, params=tuple(params))

        if row is None:
            return LookupResult(False, note="No matching row in external database.",
                                query=sql, params=tuple(params))

        record = {
            "metric_name": row[0],
            "region": row[1],
            "quarter": row[2],
            "year": row[3],
            "value": row[4],
        }
        return LookupResult(
            found=True,
            value=float(record["value"]),
            source=f"{self.kind.upper()}:{self.table} "
                   f"({record['region']} {record['quarter']} {record['year']})",
            row=record,
            query=sql,
            params=tuple(params),
        )

    def distinct_regions(self) -> List[str]:
        """``SELECT DISTINCT region`` against the external warehouse.

        Lets the region vocabulary come from the real enterprise data rather
        than the local demo file.
        """
        if not (self.enabled and self.available):
            return []
        column = self.columns.get("region", "region")
        sql = (f"SELECT DISTINCT {column} FROM {self.table} "
               f"WHERE {column} IS NOT NULL ORDER BY {column}")
        try:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute(sql)
                rows = cur.fetchall()
                cur.close()
            finally:
                conn.close()
            return [str(r[0]).strip() for r in rows if r and str(r[0]).strip()]
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []

    def latest_period(self) -> Optional[Tuple[str, int]]:
        """Newest (quarter, year) available in the warehouse."""
        if not (self.enabled and self.available):
            return None
        c = self.columns
        sql = (f"SELECT {c['quarter']}, {c['year']} FROM {self.table} "
               f"ORDER BY {c['year']} DESC, {c['quarter']} DESC")
        try:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute(sql)
                row = cur.fetchone()
                cur.close()
            finally:
                conn.close()
            return (str(row[0]), int(row[1])) if row else None
        except Exception:
            return None


# ===========================================================================
# THE FACADE THE REST OF THE APP TALKS TO
# ===========================================================================
class DataSourceManager:
    """Tries the external warehouse first, then falls back to local SQLite.

    This is the ONLY database object the verifier and the GUI ever import.
    Swapping in a different backend later means editing this one class.
    """

    def __init__(self, config) -> None:
        self.config = config
        self.local = LocalDatabase(
            path=config.local_db_path,
            table=config.get("local_database", "table", default="financial_metrics"),
        )
        self.external = ExternalDatabase(config.get("external_database", default={}))

    # -- status for the status bar ---------------------------------------
    def backend_status(self) -> str:
        if not self.external.enabled:
            return "Backend: Local SQLite only"
        if self.external.available:
            return f"Backend: {self.external.kind.upper()} (external) + SQLite fallback"
        return f"Backend: SQLite (external {self.external.kind} unavailable: {self.external.last_error[:60]})"

    def driver_report(self) -> str:
        """Human readable list of which optional drivers were importable."""
        lines = []
        for name, label in (("postgresql", "psycopg2 (PostgreSQL)"),
                            ("mysql", "pymysql (MySQL)"),
                            ("mssql", "pyodbc (SQL Server)")):
            mark = "installed" if DRIVER_AVAILABILITY.get(name) else "not installed"
            lines.append(f"  - {label}: {mark}")
        return "\n".join(lines)

    # -- the one method that matters --------------------------------------
    def lookup(
        self,
        metric: str,
        quarter: Optional[str] = None,
        year: Optional[int] = None,
        region: Optional[str] = None,
    ) -> LookupResult:
        """Resolve a metric, preferring external data, then SQLite.

        The fallback ladder is generous on purpose - a user writing
        "revenue was 4.8M" rarely specifies the region, and often gets the
        quarter slightly wrong. Rather than a blunt YELLOW we retry with
        progressively fewer constraints and *tell the user* which relaxation
        produced the match via the ``source`` string.
        """
        attempts: List[Tuple[Optional[str], Optional[int], Optional[str], str]] = [
            (quarter, year, region, "exact"),
        ]
        if region:
            attempts.append((quarter, year, None, "any-region"))
        if quarter or year:
            attempts.append((None, year, None, "any-quarter"))
            attempts.append((None, None, None, "any-period"))
        else:
            attempts.append((None, None, None, "any-period"))

        last: LookupResult = LookupResult(False, note="Metric not found.")

        for qtr, yr, reg, label in attempts:
            # 1) external first
            if self.external.enabled and self.external.available:
                res = self.external.lookup(metric, qtr, yr, reg)
                if res.found:
                    if label != "exact":
                        res.note = f"Matched with relaxed filter: {label}"
                    return res
                last = res
            # 2) local SQLite
            res = self.local.lookup(metric, qtr, yr, reg)
            if res.found:
                if label != "exact":
                    res.note = f"Matched with relaxed filter: {label}"
                return res
            last = res

        return last

    def latest_period(self) -> Optional[Tuple[str, int]]:
        """Default timeframe when a claim does not state one."""
        return self.external.latest_period() or self.local.latest_period()

    # -- region vocabulary --------------------------------------------------
    def distinct_regions(self) -> List[str]:
        """Every region label known to EITHER data source.

        This is the query that replaces the old hardcoded region list. The
        external warehouse and the local SQLite are merged (external first, so
        enterprise labels take precedence in ordering) and de-duplicated
        case-insensitively.
        """
        merged: List[str] = []
        seen: set = set()
        for label in list(self.external.distinct_regions()) + list(
                self.local.distinct_regions()):
            key = label.strip().lower()
            if key and key not in seen:
                seen.add(key)
                merged.append(label.strip())
        return merged

    def known_metrics(self) -> List[str]:
        """All metric names, used to seed the fuzzy-suggestion candidate list."""
        return self.local.distinct_metrics()


def _q(identifier: str) -> str:
    """Quote a SQLite identifier so reserved words like ``year`` are safe."""
    return f'"{identifier}"'
