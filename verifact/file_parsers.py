"""
file_parsers.py
===============

WHAT THIS FILE DOES
-------------------
Converts a file on disk into a list of :class:`~verifact.nlp_engine.Claim`
objects, following the rules in the specification:

  * ``.txt`` / ``.log`` -> read the WHOLE file as raw text and run the NLP
    extractor over it (line numbers preserved).
  * ``.csv`` / ``.xlsx`` -> read with **pandas**. Every ROW becomes a group of
    claims: for each numeric cell we take the COLUMN HEADER as the metric name
    and all the other (text) cells in that row as adjacent context.
  * ``.json`` -> walk the structure RECURSIVELY. Every key whose value is a
    number becomes a claim, with the key as the metric name and the dotted
    path as context.

ERROR HANDLING
--------------
Every parser returns a :class:`ParseResult`. If a file is corrupt, encrypted,
or in an unexpected encoding we set ``ok=False`` and put a friendly sentence in
``error`` - the GUI shows a popup and CONTINUES with the remaining files. A bad
file never aborts a batch.

ADDING A NEW FORMAT
-------------------
1. Write ``def parse_myformat(path, engine) -> ParseResult:``
2. Register the extension in :data:`PARSER_REGISTRY` at the bottom.
That's it - the GUI file dialog and the batch loop pick it up automatically.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .nlp_engine import Claim, NLPEngine
from .numbers import detect_document_profile, parse_number

# pandas is a hard requirement for CSV/XLSX but we still guard the import so
# that a user without it can at least process .txt/.log/.json files.
try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except Exception:  # pragma: no cover
    pd = None  # type: ignore
    PANDAS_AVAILABLE = False


# Extensions offered in the "Import Files" dialog.
SUPPORTED_EXTENSIONS = (".txt", ".log", ".csv", ".xlsx", ".xls", ".json")

# Encodings tried in order when reading text. Windows exports are frequently
# cp1252 while anything from a Mac/Linux pipeline is utf-8.
TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "cp1252", "latin-1")

# Cells longer than this are truncated when building row context, so one
# enormous free-text column cannot drown the metric keywords.
MAX_CELL_CONTEXT = 120


@dataclass
class ParseResult:
    """Outcome of parsing exactly one file."""

    path: str
    ok: bool = True
    claims: List[Claim] = field(default_factory=list)
    error: str = ""
    raw_text: str = ""          # kept so the detail pane can show the original
    rows_scanned: int = 0

    @property
    def filename(self) -> str:
        return os.path.basename(self.path)


# ===========================================================================
# TEXT / LOG
# ===========================================================================
def read_text_file(path: str) -> str:
    """Read a text file, trying several encodings before giving up.

    Raises ``UnicodeDecodeError``-free: the final ``latin-1`` attempt can
    decode any byte sequence, so this either returns text or raises OSError.
    """
    last_exc: Optional[Exception] = None
    for encoding in TEXT_ENCODINGS:
        try:
            with open(path, "r", encoding=encoding) as handle:
                return handle.read()
        except UnicodeDecodeError as exc:
            last_exc = exc
            continue
    if last_exc:
        raise last_exc
    return ""


def parse_text(path: str, engine: NLPEngine) -> ParseResult:
    """``.txt`` and ``.log``: the entire content is treated as raw prose."""
    try:
        text = read_text_file(path)
    except Exception as exc:
        return ParseResult(path, ok=False,
                           error=f"Could not read as text: {exc}")

    # extract_claims detects the document's number convention internally, so
    # a German or Swiss report is read correctly without any configuration.
    claims = engine.extract_claims(text)
    return ParseResult(path, ok=True, claims=claims, raw_text=text,
                       rows_scanned=text.count("\n") + 1)


# ===========================================================================
# CSV / EXCEL  (pandas)
# ===========================================================================
def _row_context(row: "pd.Series", exclude: str) -> str:
    """Build the 'adjacent text context' for one dataframe row.

    We concatenate ``header: value`` for every column except the numeric one
    we are currently verifying. That string is what the timeframe parser and
    region detector read, so a row like::

        region      | quarter | year | active_users
        Europe      | Q3      | 2024 | 984.5

    yields context "region: Europe | quarter: Q3 | year: 2024" - everything the
    verifier needs to find the right database row.
    """
    parts: List[str] = []
    for column, cell in row.items():
        if str(column) == exclude:
            continue
        if cell is None:
            continue
        text = str(cell).strip()
        if not text or text.lower() == "nan":
            continue
        parts.append(f"{column}: {text[:MAX_CELL_CONTEXT]}")
    return " | ".join(parts)


def _is_number(value: Any) -> bool:
    """True for anything pandas can hand us that behaves like a real number."""
    if value is None:
        return False
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return number == number and abs(number) != float("inf")  # reject NaN/inf


def _coerce_cell(value: Any, profile) -> Optional[float]:
    """Turn a spreadsheet cell into a float, honouring international formats.

    WHY THIS EXISTS
    ---------------
    pandas types a column as ``object`` (text) the moment one cell is not a
    plain US-format number. A German export whose revenue column reads
    ``1.234.567,89`` therefore arrives as STRINGS, and the old code skipped it
    entirely - the file looked like it had no numbers at all.

    We first accept anything already numeric, then fall back to the global
    normalizer for text cells. ``profile`` is the convention detected for the
    whole file, so ambiguous cells resolve consistently.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if _is_number(value) else None

    text = str(value).strip()
    if not text or text.lower() in ("nan", "none", "null", "-", "n/a", "na"):
        return None

    parsed = parse_number(text, profile)
    if parsed is None:
        return None
    return parsed.scaled()


def _is_json_number(value: Any) -> bool:
    """Stricter than :func:`_is_number` - the value must be a REAL JSON number.

    JSON preserves the distinction between ``"year": 2024`` and
    ``"year": "2024"``. The quoted form is metadata the author chose to write
    as text, so we leave it alone; only genuine ``int``/``float`` leaves become
    claims. Booleans are excluded because ``True`` is ``1`` in Python.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# Keys that are structural metadata rather than measurements. A JSON document
# almost always carries a "year" or "id" field, and reporting it as a business
# claim is pure noise. Add your own here.
JSON_METADATA_KEYS = {
    "year", "yr", "fiscal_year", "id", "index", "timestamp", "ts",
    "version", "count_of_pages", "page", "code", "status_code",
}


def _dataframe_to_claims(frame: "pd.DataFrame", engine: NLPEngine,
                         sheet: str = "") -> List[Claim]:
    """Shared logic for CSV and Excel: one claim per numeric cell.

    The specification says "treat each row as a separate claim". In practice a
    row can hold several metrics (users AND revenue), so we emit one claim per
    numeric cell but tag every claim with the same row number - the results
    table then groups naturally by row.
    """
    claims: List[Claim] = []

    # Detect the number convention from the WHOLE sheet before reading any
    # cell, so "1.234,56" in a German export is not mistaken for 1.234.
    try:
        sample = frame.head(200).to_string(index=False)
    except Exception:
        sample = ""
    profile = detect_document_profile(sample)
    engine.number_profile = profile

    for position, (_, row) in enumerate(frame.iterrows(), start=1):
        for column in frame.columns:
            cell = row[column]
            numeric = _coerce_cell(cell, profile)
            if numeric is None:
                continue

            header = str(column).strip()
            # Skip obvious bookkeeping columns - they are never claims.
            if header.lower() in ("id", "index", "row", "#", "unnamed: 0"):
                continue
            # A 'year' column is metadata, not a metric.
            if header.lower() in ("year", "yr", "fiscal_year"):
                continue

            context = _row_context(row, exclude=header)
            label = f"{sheet}:{header}" if sheet else header

            claim = engine.extract_from_value(
                value=numeric,
                label=header,
                context=f"{header}: {cell} | {context}",
                line_no=position + 1,  # +1 because row 1 is the header
            )
            # Preserve how the cell was actually written, so the report can
            # show "1.234.567,89" rather than a normalised 1234567.89.
            claim.raw_value = str(cell)
            claim.number_profile = profile.name
            claim.source_label = label
            claims.append(claim)

    return claims


def parse_csv(path: str, engine: NLPEngine) -> ParseResult:
    """``.csv``: pandas read, then numeric-cell extraction."""
    if not PANDAS_AVAILABLE:
        return ParseResult(path, ok=False,
                           error="pandas is not installed - cannot read CSV files.")
    frame = None
    last_exc: Optional[Exception] = None
    for encoding in TEXT_ENCODINGS:
        try:
            frame = pd.read_csv(path, encoding=encoding)
            break
        except UnicodeDecodeError as exc:
            last_exc = exc
            continue
        except Exception as exc:
            return ParseResult(path, ok=False, error=f"CSV parse failed: {exc}")

    if frame is None:
        return ParseResult(path, ok=False,
                           error=f"CSV encoding not recognised: {last_exc}")

    try:
        claims = _dataframe_to_claims(frame, engine)
        preview = frame.head(50).to_string(index=False)
    except Exception as exc:
        return ParseResult(path, ok=False, error=f"CSV analysis failed: {exc}")

    return ParseResult(path, ok=True, claims=claims, raw_text=preview,
                       rows_scanned=len(frame))


def parse_excel(path: str, engine: NLPEngine) -> ParseResult:
    """``.xlsx``/``.xls``: every sheet is scanned, sheet name kept as context."""
    if not PANDAS_AVAILABLE:
        return ParseResult(path, ok=False,
                           error="pandas is not installed - cannot read Excel files.")
    try:
        sheets: Dict[str, "pd.DataFrame"] = pd.read_excel(path, sheet_name=None)
    except Exception as exc:
        return ParseResult(
            path, ok=False,
            error=f"Excel parse failed: {exc}. Is openpyxl installed and the file uncorrupted?")

    claims: List[Claim] = []
    previews: List[str] = []
    total_rows = 0

    for sheet_name, frame in sheets.items():
        try:
            claims.extend(_dataframe_to_claims(frame, engine, sheet=str(sheet_name)))
            previews.append(f"--- sheet: {sheet_name} ---\n"
                            f"{frame.head(30).to_string(index=False)}")
            total_rows += len(frame)
        except Exception as exc:
            previews.append(f"--- sheet: {sheet_name} FAILED: {exc} ---")

    return ParseResult(path, ok=True, claims=claims,
                       raw_text="\n\n".join(previews), rows_scanned=total_rows)


# ===========================================================================
# JSON
# ===========================================================================
def _walk_json(node: Any, engine: NLPEngine, claims: List[Claim],
               path_parts: List[str], sibling_text: str = "") -> None:
    """Recursively descend a JSON structure collecting numeric leaves.

    For every ``{"revenue": 4800000}`` we create a claim whose metric name is
    the KEY and whose context is the dotted path plus any *text* siblings in
    the same object (which is where "region": "Europe" and "quarter": "Q3"
    normally live).
    """
    if isinstance(node, dict):
        # Collect the string values in this object once - they are the shared
        # context for every numeric key at this level.
        # Note we deliberately include stringified values such as
        # ``"year": "2024"`` here: they are not claims themselves, but they ARE
        # exactly the context the timeframe parser needs.
        siblings = " | ".join(
            f"{k}: {v}" for k, v in node.items()
            if isinstance(v, (str, bool)) and str(v).strip()
        )
        combined_siblings = " | ".join(p for p in (sibling_text, siblings) if p)

        for key, value in node.items():
            new_path = path_parts + [str(key)]
            if isinstance(value, (dict, list)):
                _walk_json(value, engine, claims, new_path, combined_siblings)
            elif _is_json_number(value) and str(key).lower() not in JSON_METADATA_KEYS:
                dotted = ".".join(new_path)
                context = f"{dotted} = {value}"
                if combined_siblings:
                    context += f" | {combined_siblings}"
                claim = engine.extract_from_value(
                    value=float(value),
                    label=str(key),
                    context=context,
                    line_no=len(claims) + 1,
                )
                claim.source_label = dotted
                claims.append(claim)

    elif isinstance(node, list):
        for index, item in enumerate(node):
            _walk_json(item, engine, claims, path_parts + [f"[{index}]"], sibling_text)


def parse_json(path: str, engine: NLPEngine) -> ParseResult:
    """``.json``: recursive traversal; every numeric value becomes a claim."""
    try:
        text = read_text_file(path)
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return ParseResult(path, ok=False,
                           error=f"Invalid JSON (line {exc.lineno}): {exc.msg}")
    except Exception as exc:
        return ParseResult(path, ok=False, error=f"Could not read JSON: {exc}")

    claims: List[Claim] = []
    _walk_json(data, engine, claims, [])
    return ParseResult(path, ok=True, claims=claims, raw_text=text[:20000],
                       rows_scanned=len(claims))


# ===========================================================================
# REGISTRY + DISPATCHER
# ===========================================================================
PARSER_REGISTRY: Dict[str, Callable[[str, NLPEngine], ParseResult]] = {
    ".txt": parse_text,
    ".log": parse_text,
    ".md": parse_text,
    ".csv": parse_csv,
    ".xlsx": parse_excel,
    ".xls": parse_excel,
    ".json": parse_json,
}


def parse_file(path: str, engine: NLPEngine) -> ParseResult:
    """Pick the right parser for ``path`` and run it defensively.

    This function NEVER raises: any unexpected exception is converted into a
    failed :class:`ParseResult` so the batch loop can keep going.
    """
    if not os.path.exists(path):
        return ParseResult(path, ok=False, error="File does not exist.")
    if os.path.isdir(path):
        return ParseResult(path, ok=False, error="This is a folder, not a file.")

    try:
        if os.path.getsize(path) == 0:
            return ParseResult(path, ok=False, error="File is empty.")
    except OSError as exc:
        return ParseResult(path, ok=False, error=f"Cannot stat file: {exc}")

    extension = os.path.splitext(path)[1].lower()
    parser = PARSER_REGISTRY.get(extension)
    if parser is None:
        return ParseResult(
            path, ok=False,
            error=f"Unsupported file type '{extension}'. "
                  f"Supported: {', '.join(sorted(PARSER_REGISTRY))}")

    try:
        return parser(path, engine)
    except Exception as exc:  # last line of defence
        return ParseResult(path, ok=False,
                           error=f"Unexpected error ({type(exc).__name__}): {exc}")
