"""
migrations/migrate.py
=====================

Standalone migration runner for ``verifact.db``.

You normally do NOT need this: Veri-Fact applies the additive migrations
(001 and 002) automatically at startup. This script exists for three cases:

  * you want to upgrade a database WITHOUT launching the GUI;
  * you are a DBA who wants to see exactly what runs, and when;
  * you want the optional cosmetic region rename (003) from the command line.

USAGE
-----
    python migrations/migrate.py                  # show status, apply 001+002
    python migrations/migrate.py --status         # report only, change nothing
    python migrations/migrate.py --all            # also apply 003 (renames rows)
    python migrations/migrate.py --db path/to.db  # target a specific file

SAFETY
------
* 001 and 002 are purely ADDITIVE - they create tables, never alter or drop.
* 003 MODIFIES ROWS and is therefore opt-in behind ``--all``, with a typed
  confirmation prompt.
* A timestamped backup is taken automatically before anything is written.
* Every step runs in a transaction and is idempotent.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from typing import List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(os.path.dirname(HERE), "verifact.db")

# Additive migrations - always safe, applied by default.
ADDITIVE = ["001_add_schema_meta.sql", "002_add_metric_aliases.sql"]
# Destructive/cosmetic - opt-in only.
OPTIONAL = ["003_neutral_region_labels.sql"]


def backup(path: str) -> str:
    """Copy the database aside before touching it. Returns the backup path."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = f"{path}.{stamp}.backup"
    shutil.copy2(path, target)
    return target


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def report(path: str) -> None:
    """Print what the database currently looks like."""
    if not os.path.exists(path):
        print(f"  Database not found: {path}")
        print("  Nothing to migrate - launch the app once and it will create "
              "and seed a fresh one.")
        return

    conn = sqlite3.connect(path)
    try:
        print(f"  Database        : {path}")
        print(f"  Size            : {os.path.getsize(path):,} bytes")

        for table in ("financial_metrics", "schema_meta", "metric_aliases"):
            if table_exists(conn, table):
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                print(f"  {table:16}: present ({count} rows)")
            else:
                print(f"  {table:16}: MISSING")

        if table_exists(conn, "schema_meta"):
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            print(f"  schema_version  : {row[0] if row else 'unset'}")

        if table_exists(conn, "financial_metrics"):
            regions = [r[0] for r in conn.execute(
                "SELECT DISTINCT region FROM financial_metrics ORDER BY region")]
            print(f"  regions in data : {', '.join(regions) or '(none)'}")
            legacy = conn.execute(
                "SELECT COUNT(*) FROM financial_metrics "
                "WHERE region IN ('Europe','North America')").fetchone()[0]
            if legacy:
                print(f"  legacy labels   : {legacy} row(s) still use "
                      f"Europe / North America")
                print("                    (optional: run with --all to rename, "
                      "or use Help > Migrate Legacy Region Labels in the app)")
    finally:
        conn.close()


def apply_script(conn: sqlite3.Connection, filename: str) -> Tuple[bool, str]:
    """Execute one .sql file. Returns ``(ok, message)``; never raises."""
    path = os.path.join(HERE, filename)
    if not os.path.exists(path):
        return False, f"script not found: {filename}"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            conn.executescript(handle.read())
        return True, "applied"
    except sqlite3.Error as exc:
        return False, f"FAILED: {exc}"


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply Veri-Fact Desktop database migrations.")
    parser.add_argument("--db", default=DEFAULT_DB,
                        help="path to verifact.db (default: alongside main.py)")
    parser.add_argument("--status", action="store_true",
                        help="report current state and exit without changes")
    parser.add_argument("--all", action="store_true",
                        help="also apply the optional region-label rename (003)")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt for --all")
    args = parser.parse_args(argv)

    print("Veri-Fact Desktop - database migration")
    print("=" * 62)
    report(args.db)
    print("=" * 62)

    if args.status:
        return 0

    if not os.path.exists(args.db):
        print("Nothing to do.")
        return 0

    scripts = list(ADDITIVE)

    if args.all:
        print("\nMigration 003 RENAMES EXISTING ROWS:")
        print("    Europe        -> Region_A")
        print("    North America -> Region_B")
        print("Only do this if those are DEMO rows. If they are your real")
        print("figures, keep your own labels - Veri-Fact reads regions from")
        print("the data, so they work fine as they are.")
        if not args.yes:
            answer = input("\nType 'yes' to include migration 003: ").strip().lower()
            if answer != "yes":
                print("Skipping 003 - applying additive migrations only.")
            else:
                scripts += OPTIONAL
        else:
            scripts += OPTIONAL

    saved = backup(args.db)
    print(f"\nBackup written to: {saved}")

    conn = sqlite3.connect(args.db)
    failures = 0
    try:
        for name in scripts:
            ok, message = apply_script(conn, name)
            print(f"  {name:34} {message}")
            if not ok:
                failures += 1
    finally:
        conn.close()

    print("=" * 62)
    report(args.db)
    print("=" * 62)

    if failures:
        print(f"{failures} migration(s) failed. Your backup is at:\n  {saved}")
        return 1
    print("Migration complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
