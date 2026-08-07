"""
compare.py
==========

TWO-FILE COMPARISON ("draft vs final")
--------------------------------------
A pure DIFF tool. It answers "what changed between these two documents?" and
DELIBERATELY NEVER TOUCHES THE DATABASE - there is no notion of truth here,
only of difference. That makes it useful for the very common task of checking
a draft deck against the final one, or last month's export against this
month's.

HOW ALIGNMENT WORKS
-------------------
Both files are run through the ordinary claim extractor, then every claim is
reduced to an alignment key:

        (metric, region, timeframe)

Claims from file A and file B that share a key are paired. Each pair yields a
row with both values plus the absolute and percentage delta. Keys present in
only one file are reported as ONLY_IN_A / ONLY_IN_B, which is often the most
interesting finding - a number that quietly vanished between drafts.

DUPLICATE KEYS
--------------
A document can state the same metric twice (a summary and a detail table). We
pair duplicates positionally in document order, so the first mention in A
aligns with the first in B. Any surplus becomes an unmatched row rather than
being silently dropped.

STABILITY
---------
:func:`compare_files` never raises. Unreadable files come back as a
:class:`ComparisonReport` with ``ok=False`` and a friendly message.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .file_parsers import ParseResult, parse_file
from .nlp_engine import Claim, NLPEngine

# Row status values.
STATUS_SAME = "SAME"
STATUS_CHANGED = "CHANGED"
STATUS_ONLY_A = "ONLY_IN_A"
STATUS_ONLY_B = "ONLY_IN_B"

#: Colours reused by the GUI and the Excel exporter.
COMPARE_COLOURS: Dict[str, str] = {
    STATUS_SAME: "#E4F6E4",
    STATUS_CHANGED: "#FCE4E4",
    STATUS_ONLY_A: "#FFF6DC",
    STATUS_ONLY_B: "#E3ECFA",
}


@dataclass
class ComparisonRow:
    """One aligned (or orphaned) metric across the two files."""

    metric: str = ""
    region: str = ""
    timeframe: str = ""
    value_a: Optional[float] = None
    value_b: Optional[float] = None
    delta_absolute: Optional[float] = None
    delta_percent: Optional[float] = None
    status: str = STATUS_SAME
    context_a: str = ""
    context_b: str = ""
    line_a: int = 0
    line_b: int = 0

    def as_tuple(self) -> Tuple[str, ...]:
        """Column order for the Compare tab's Treeview."""
        return (
            self.metric or "(unknown)",
            self.region or "-",
            self.timeframe or "-",
            _fmt(self.value_a),
            _fmt(self.value_b),
            _fmt(self.delta_absolute),
            "-" if self.delta_percent is None else f"{self.delta_percent:+.2f}%",
            self.status,
        )

    def to_dict(self) -> Dict[str, object]:
        """Flat form for CSV / JSON / Excel export."""
        return {
            "Metric": self.metric,
            "Region": self.region,
            "Timeframe": self.timeframe,
            "Source A Value": self.value_a,
            "Source B Value": self.value_b,
            "Delta": self.delta_absolute,
            "Delta %": (round(self.delta_percent, 4)
                        if self.delta_percent is not None else None),
            "Status": self.status,
            "Line A": self.line_a,
            "Line B": self.line_b,
            "Context A": self.context_a,
            "Context B": self.context_b,
        }


@dataclass
class ComparisonReport:
    """Everything the Compare tab needs to render a run."""

    ok: bool = True
    file_a: str = ""
    file_b: str = ""
    rows: List[ComparisonRow] = field(default_factory=list)
    error: str = ""
    warnings: List[str] = field(default_factory=list)

    def summary(self) -> Dict[str, int]:
        tally = {STATUS_SAME: 0, STATUS_CHANGED: 0,
                 STATUS_ONLY_A: 0, STATUS_ONLY_B: 0}
        for row in self.rows:
            tally[row.status] = tally.get(row.status, 0) + 1
        tally["TOTAL"] = len(self.rows)
        return tally


def _alignment_key(claim: Claim) -> Tuple[str, str, str]:
    """Reduce a claim to the tuple used for pairing across files.

    Normalised to lowercase so "Revenue" and "revenue" align, and using the
    RESOLVED timeframe so "last quarter" in the draft aligns with "Q3 2024"
    spelled out in the final.
    """
    metric = (claim.metric_column or claim.metric_text or "").strip().lower()
    region = (claim.region or "").strip().lower()
    timeframe = claim.timeframe.describe().strip().lower()
    # Strip our own annotations so "(assumed)" never blocks a match.
    for noise in (" (assumed)", " (default)"):
        timeframe = timeframe.replace(noise, "")
    return metric, region, timeframe


def _group_claims(claims: List[Claim]) -> Dict[Tuple[str, str, str], List[Claim]]:
    """Bucket claims by alignment key, preserving document order."""
    grouped: Dict[Tuple[str, str, str], List[Claim]] = {}
    for claim in claims:
        grouped.setdefault(_alignment_key(claim), []).append(claim)
    return grouped


def _delta(value_a: float, value_b: float) -> Tuple[float, Optional[float]]:
    """Absolute and percentage change from A to B.

    Percentage is relative to A (the baseline/draft), which is the convention
    people expect when reading "how much did this move?". When A is zero a
    percentage is meaningless, so we return ``None`` rather than infinity.
    """
    absolute = value_b - value_a
    if value_a == 0:
        return absolute, None
    return absolute, (absolute / abs(value_a)) * 100.0


def compare_claim_sets(claims_a: List[Claim], claims_b: List[Claim],
                       tolerance_percent: float = 0.0) -> List[ComparisonRow]:
    """Align two lists of claims and produce comparison rows.

    ``tolerance_percent`` lets tiny rounding differences count as SAME; leave
    it at 0 for an exact diff.
    """
    grouped_a = _group_claims(claims_a)
    grouped_b = _group_claims(claims_b)
    rows: List[ComparisonRow] = []

    all_keys = list(grouped_a)
    for key in grouped_b:
        if key not in grouped_a:
            all_keys.append(key)

    for key in all_keys:
        metric, region, timeframe = key
        bucket_a = grouped_a.get(key, [])
        bucket_b = grouped_b.get(key, [])

        # Pair positionally; surplus entries become one-sided rows.
        for index in range(max(len(bucket_a), len(bucket_b))):
            claim_a = bucket_a[index] if index < len(bucket_a) else None
            claim_b = bucket_b[index] if index < len(bucket_b) else None

            row = ComparisonRow(
                metric=metric or "(unknown)",
                region=region,
                timeframe=timeframe,
            )

            if claim_a is not None:
                row.value_a = claim_a.scaled_value()
                row.context_a = claim_a.context
                row.line_a = claim_a.line_no
            if claim_b is not None:
                row.value_b = claim_b.scaled_value()
                row.context_b = claim_b.context
                row.line_b = claim_b.line_no

            if claim_a is None:
                row.status = STATUS_ONLY_B
            elif claim_b is None:
                row.status = STATUS_ONLY_A
            else:
                absolute, percent = _delta(row.value_a, row.value_b)
                row.delta_absolute = absolute
                row.delta_percent = percent
                within = (abs(percent) <= tolerance_percent
                          if percent is not None else absolute == 0)
                row.status = STATUS_SAME if within else STATUS_CHANGED

            rows.append(row)

    # Most useful ordering: real changes first, then orphans, then matches.
    priority = {STATUS_CHANGED: 0, STATUS_ONLY_A: 1, STATUS_ONLY_B: 2, STATUS_SAME: 3}
    rows.sort(key=lambda r: (priority.get(r.status, 9), r.metric, r.timeframe))
    return rows


def compare_files(path_a: str, path_b: str, engine: NLPEngine,
                  tolerance_percent: float = 0.0) -> ComparisonReport:
    """Parse both files and diff them. Never raises.

    Returns a report whose ``ok`` flag tells the GUI whether to show the table
    or an error message.
    """
    report = ComparisonReport(
        file_a=os.path.basename(path_a) if path_a else "",
        file_b=os.path.basename(path_b) if path_b else "",
    )

    if not path_a or not path_b:
        report.ok = False
        report.error = "Please choose two files to compare."
        return report

    if os.path.abspath(path_a) == os.path.abspath(path_b):
        report.ok = False
        report.error = "Both slots hold the same file - choose two different files."
        return report

    try:
        parsed_a: ParseResult = parse_file(path_a, engine)
        parsed_b: ParseResult = parse_file(path_b, engine)
    except Exception as exc:  # parse_file is already defensive, but be sure
        report.ok = False
        report.error = f"Unexpected error while reading the files: {exc}"
        return report

    if not parsed_a.ok:
        report.ok = False
        report.error = f"Could not read '{report.file_a}': {parsed_a.error}"
        return report
    if not parsed_b.ok:
        report.ok = False
        report.error = f"Could not read '{report.file_b}': {parsed_b.error}"
        return report

    if not parsed_a.claims:
        report.warnings.append(f"No numeric claims found in '{report.file_a}'.")
    if not parsed_b.claims:
        report.warnings.append(f"No numeric claims found in '{report.file_b}'.")

    report.rows = compare_claim_sets(parsed_a.claims, parsed_b.claims,
                                     tolerance_percent)
    if not report.rows:
        report.warnings.append("Neither file produced anything to compare.")

    return report


def _fmt(value: Optional[float]) -> str:
    """Readable number for the comparison table."""
    if value is None:
        return "-"
    if abs(value - round(value)) < 1e-9 and abs(value) >= 1000:
        return f"{value:,.0f}"
    if abs(value) >= 1000:
        return f"{value:,.2f}"
    return f"{value:,.4f}".rstrip("0").rstrip(".") or "0"
