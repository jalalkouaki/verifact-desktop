"""
exporters.py
============

WHAT THIS FILE DOES
-------------------
Writes the list of :class:`~verifact.verifier.VerificationResult` objects to
disk in three formats:

  * ``.csv``  - every column, plain text, opens anywhere.
  * ``.xlsx`` - a formatted report where each row is COLOUR CODED green /
    amber / red according to its verdict, with a frozen bold header, auto-sized
    columns, an autofilter and a summary sheet. Built with **openpyxl**.
  * ``.json`` - a machine-readable audit log including a metadata header
    (timestamp, tool version, tolerance used, backend names).

Every function returns ``(ok: bool, message: str)`` so the GUI can show a
single consistent success/error popup.
"""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Sequence, Tuple

from .verifier import (STATUS_COLOURS, STATUS_FAIL, STATUS_PASS,
                       STATUS_TEXT_COLOURS, STATUS_WARN, VerificationResult,
                       summarise)

try:
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    OPENPYXL_AVAILABLE = True
except Exception:  # pragma: no cover
    openpyxl = None  # type: ignore
    OPENPYXL_AVAILABLE = False


# The canonical column order used by every export format.
EXPORT_COLUMNS: Sequence[str] = (
    "Filename",
    "Line/Row",
    "Claimed Value",
    "Claimed (as written)",
    "Metric",
    "Metric Text",
    "Region",
    "Timeframe",
    "Actual Value",
    "Difference %",
    "Status",
    "Source",
    "Interpretation",
    "Formula",
    "Formula Working",
    "Validation Note",
    "Suggested Metric",
    "Ambiguous Format",
    "Reason",
    "Context",
    "SQL Query",
)

#: Column order for the Compare tab's export.
COMPARE_COLUMNS: Sequence[str] = (
    "Metric",
    "Region",
    "Timeframe",
    "Source A Value",
    "Source B Value",
    "Delta",
    "Delta %",
    "Status",
    "Line A",
    "Line B",
    "Context A",
    "Context B",
)


def _rows(results: List[VerificationResult]) -> List[Dict[str, Any]]:
    return [item.to_dict() for item in results]


# ===========================================================================
# CSV
# ===========================================================================
def export_csv(results: List[VerificationResult], path: str) -> Tuple[bool, str]:
    """Write every result and every column to a UTF-8 CSV file.

    ``newline=""`` plus ``utf-8-sig`` means Excel on Windows opens the file
    with correct encoding and no blank lines between rows.
    """
    try:
        with open(path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(EXPORT_COLUMNS),
                                    extrasaction="ignore")
            writer.writeheader()
            for row in _rows(results):
                writer.writerow(row)
        return True, f"Saved {len(results)} rows to {os.path.basename(path)}"
    except Exception as exc:
        return False, f"CSV export failed: {exc}"


# ===========================================================================
# JSON
# ===========================================================================
def export_json(results: List[VerificationResult], path: str,
                metadata: Dict[str, Any] | None = None) -> Tuple[bool, str]:
    """Write a structured audit log: metadata header + one object per result."""
    payload = {
        "tool": "Veri-Fact Desktop",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": summarise(results),
        "metadata": metadata or {},
        "results": _rows(results),
    }
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, default=str)
        return True, f"Saved {len(results)} rows to {os.path.basename(path)}"
    except Exception as exc:
        return False, f"JSON export failed: {exc}"


# ===========================================================================
# EXCEL  (colour coded)
# ===========================================================================
def export_xlsx(results: List[VerificationResult], path: str,
                metadata: Dict[str, Any] | None = None) -> Tuple[bool, str]:
    """Write a presentation-quality .xlsx report with colour coded rows.

    Sheet 1 "Results" - one row per claim, filled green/amber/red.
    Sheet 2 "Summary" - counts per verdict plus the run metadata, so whoever
    receives the file can see the configuration that produced it.
    """
    if not OPENPYXL_AVAILABLE:
        return False, ("openpyxl is not installed. Run:  pip install openpyxl\n"
                       "You can still export to CSV or JSON.")

    try:
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Results"

        thin = Side(style="thin", color="D0D0D0")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)

        # --- header row ---------------------------------------------------
        header_fill = PatternFill("solid", fgColor="2F4F6F")
        header_font = Font(bold=True, color="FFFFFF", size=11)
        for column_index, name in enumerate(EXPORT_COLUMNS, start=1):
            cell = sheet.cell(row=1, column=column_index, value=name)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = border

        # --- data rows, colour coded by verdict -----------------------------
        for row_index, record in enumerate(_rows(results), start=2):
            status = str(record.get("Status", STATUS_WARN))
            fill = PatternFill("solid", fgColor=STATUS_COLOURS.get(
                status, "#FFFFFF").lstrip("#"))
            font = Font(color=STATUS_TEXT_COLOURS.get(status, "#000000").lstrip("#"))

            for column_index, name in enumerate(EXPORT_COLUMNS, start=1):
                value = record.get(name, "")
                cell = sheet.cell(row=row_index, column=column_index, value=value)
                cell.fill = fill
                cell.font = font
                cell.border = border
                if name in ("Claimed Value", "Actual Value", "Difference %"):
                    cell.alignment = Alignment(horizontal="right")
                    if isinstance(value, (int, float)):
                        cell.number_format = "#,##0.####"
                elif name in ("Reason", "Context", "SQL Query"):
                    cell.alignment = Alignment(vertical="top", wrap_text=True)

        # --- cosmetics ------------------------------------------------------
        widths = {"Filename": 26, "Line/Row": 9, "Claimed Value": 15,
                  "Claimed (as written)": 18, "Metric": 18, "Metric Text": 16,
                  "Region": 14, "Timeframe": 16, "Actual Value": 15,
                  "Difference %": 12, "Status": 10, "Source": 34,
                  "Interpretation": 13, "Formula": 30, "Formula Working": 36,
                  "Validation Note": 40, "Suggested Metric": 16,
                  "Ambiguous Format": 14, "Reason": 55, "Context": 60,
                  "SQL Query": 60}
        for column_index, name in enumerate(EXPORT_COLUMNS, start=1):
            sheet.column_dimensions[get_column_letter(column_index)].width = \
                widths.get(name, 18)
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = (
            f"A1:{get_column_letter(len(EXPORT_COLUMNS))}{max(1, len(results) + 1)}"
        )

        # --- summary sheet ---------------------------------------------------
        tally = summarise(results)
        summary = workbook.create_sheet("Summary")
        summary["A1"] = "Veri-Fact Desktop - Verification Report"
        summary["A1"].font = Font(bold=True, size=14)
        summary["A3"] = "Generated"
        summary["B3"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        rows_out = [
            ("Total claims checked", tally.get("TOTAL", 0), None),
            ("PASS  (green)", tally.get(STATUS_PASS, 0), STATUS_PASS),
            ("FAIL  (red)", tally.get(STATUS_FAIL, 0), STATUS_FAIL),
            ("WARNING (amber)", tally.get(STATUS_WARN, 0), STATUS_WARN),
        ]
        for offset, (label, value, status) in enumerate(rows_out, start=5):
            summary.cell(row=offset, column=1, value=label).font = Font(bold=True)
            cell = summary.cell(row=offset, column=2, value=value)
            if status:
                cell.fill = PatternFill(
                    "solid", fgColor=STATUS_COLOURS[status].lstrip("#"))

        next_row = 11
        for key, value in (metadata or {}).items():
            summary.cell(row=next_row, column=1, value=str(key)).font = Font(bold=True)
            summary.cell(row=next_row, column=2, value=str(value))
            next_row += 1

        summary.column_dimensions["A"].width = 28
        summary.column_dimensions["B"].width = 70

        workbook.save(path)
        return True, f"Saved {len(results)} rows to {os.path.basename(path)}"
    except Exception as exc:
        return False, f"Excel export failed: {exc}"


# ===========================================================================
# COMPARISON EXPORT  (Compare tab)
# ===========================================================================
def export_comparison(report, path: str) -> Tuple[bool, str]:
    """Write a two-file comparison to CSV, colour-coded XLSX, or JSON.

    ``report`` is a :class:`verifact.compare.ComparisonReport`. Rows are tinted
    by status: red for CHANGED, green for SAME, amber/blue for values present
    in only one of the two documents.
    """
    rows = [row.to_dict() for row in getattr(report, "rows", [])]
    if not rows:
        return False, "There is nothing to export - run a comparison first."

    extension = os.path.splitext(path)[1].lower()

    try:
        if extension == ".csv":
            with open(path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(COMPARE_COLUMNS),
                                        extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
            return True, f"Saved {len(rows)} comparison rows to {os.path.basename(path)}"

        if extension == ".json":
            payload = {
                "tool": "Veri-Fact Desktop - comparison",
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "source_a": getattr(report, "file_a", ""),
                "source_b": getattr(report, "file_b", ""),
                "summary": report.summary(),
                "rows": rows,
            }
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, default=str)
            return True, f"Saved {len(rows)} comparison rows to {os.path.basename(path)}"

        if extension in (".xlsx", ".xlsm"):
            if not OPENPYXL_AVAILABLE:
                return False, ("openpyxl is not installed. Run: pip install openpyxl\n"
                               "You can still export the comparison to CSV or JSON.")
            # Imported here so a missing package cannot break module import.
            from .compare import COMPARE_COLOURS

            workbook = openpyxl.Workbook()
            sheet = workbook.active
            sheet.title = "Comparison"

            header_fill = PatternFill("solid", fgColor="2F4F6F")
            header_font = Font(bold=True, color="FFFFFF", size=11)
            for index, name in enumerate(COMPARE_COLUMNS, start=1):
                cell = sheet.cell(row=1, column=index, value=name)
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center")

            for row_index, record in enumerate(rows, start=2):
                colour = COMPARE_COLOURS.get(
                    str(record.get("Status", "")), "#FFFFFF").lstrip("#")
                fill = PatternFill("solid", fgColor=colour)
                for column_index, name in enumerate(COMPARE_COLUMNS, start=1):
                    value = record.get(name, "")
                    cell = sheet.cell(row=row_index, column=column_index, value=value)
                    cell.fill = fill
                    if name in ("Source A Value", "Source B Value", "Delta", "Delta %"):
                        cell.alignment = Alignment(horizontal="right")
                        if isinstance(value, (int, float)):
                            cell.number_format = "#,##0.####"

            widths = {"Metric": 22, "Region": 14, "Timeframe": 16,
                      "Source A Value": 16, "Source B Value": 16, "Delta": 14,
                      "Delta %": 11, "Status": 12, "Line A": 8, "Line B": 8,
                      "Context A": 60, "Context B": 60}
            for index, name in enumerate(COMPARE_COLUMNS, start=1):
                sheet.column_dimensions[get_column_letter(index)].width = \
                    widths.get(name, 18)
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = (
                f"A1:{get_column_letter(len(COMPARE_COLUMNS))}{len(rows) + 1}")

            summary_sheet = workbook.create_sheet("Summary")
            summary_sheet["A1"] = "Veri-Fact Desktop - Document Comparison"
            summary_sheet["A1"].font = Font(bold=True, size=14)
            summary_sheet["A3"] = "Source A"
            summary_sheet["B3"] = getattr(report, "file_a", "")
            summary_sheet["A4"] = "Source B"
            summary_sheet["B4"] = getattr(report, "file_b", "")
            summary_sheet["A5"] = "Generated"
            summary_sheet["B5"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for offset, (key, value) in enumerate(report.summary().items(), start=7):
                summary_sheet.cell(row=offset, column=1, value=str(key)).font = Font(bold=True)
                summary_sheet.cell(row=offset, column=2, value=value)
            summary_sheet.column_dimensions["A"].width = 22
            summary_sheet.column_dimensions["B"].width = 60

            workbook.save(path)
            return True, f"Saved {len(rows)} comparison rows to {os.path.basename(path)}"

        return False, f"Unknown export extension '{extension}'. Use .csv, .xlsx or .json."
    except Exception as exc:
        return False, f"Comparison export failed: {exc}"


def export_auto(results: List[VerificationResult], path: str,
                metadata: Dict[str, Any] | None = None) -> Tuple[bool, str]:
    """Choose the exporter based on the file extension the user typed."""
    extension = os.path.splitext(path)[1].lower()
    if extension == ".csv":
        return export_csv(results, path)
    if extension in (".xlsx", ".xlsm"):
        return export_xlsx(results, path, metadata)
    if extension == ".json":
        return export_json(results, path, metadata)
    return False, (f"Unknown export extension '{extension}'. "
                   f"Use .csv, .xlsx or .json.")
