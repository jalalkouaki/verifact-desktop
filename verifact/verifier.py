"""
verifier.py
===========

WHAT THIS FILE DOES
-------------------
Takes a :class:`~verifact.nlp_engine.Claim` (a number somebody asserted) and a
:class:`~verifact.database.DataSourceManager` (the truth) and produces a
verdict:

    GREEN  / PASS    - |claimed - actual| / actual * 100  <  tolerance (1%)
    RED    / FAIL    - the difference exceeds the tolerance. We report the
                       correct number AND the row it came from.
    YELLOW / WARNING - we could not check it: unknown metric, unknown region,
                       unknown timeframe, or no number could be extracted.

THE "SCALE" PROBLEM (important, read this)
------------------------------------------
People write "984.5k users" but the database stores ``984.5`` (meaning
thousands). A naive comparison of 984 500 against 984.5 would produce a false
FAIL. So before declaring a mismatch we test a small set of *equivalent
interpretations* of the same claim:

    literal  984.5
    scaled   984.5 * 1000 = 984500
    de-scaled 984.5 / 1000 = 0.9845

If ANY interpretation lands inside the tolerance we call it a PASS and record
which interpretation matched, so the audit trail stays honest. This is the
single most important piece of real-world robustness in the whole tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .database import DataSourceManager, LookupResult
from .formulas import FormulaEngine, FormulaResult
from .nlp_engine import Claim

# The three verdicts, used as Treeview tags and Excel fill colours.
STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_WARN = "WARNING"

STATUS_COLOURS: Dict[str, str] = {
    STATUS_PASS: "#C6EFCE",   # light green
    STATUS_FAIL: "#FFC7CE",   # light red
    STATUS_WARN: "#FFEB9C",   # light amber
}
STATUS_TEXT_COLOURS: Dict[str, str] = {
    STATUS_PASS: "#006100",
    STATUS_FAIL: "#9C0006",
    STATUS_WARN: "#9C6500",
}


@dataclass
class VerificationResult:
    """One row in the results table - everything the GUI and exporters need."""

    filename: str = "(pasted text)"
    line_no: int = 0
    claimed_display: str = ""          # "984.5k" exactly as written
    claimed_value: Optional[float] = None
    metric_text: str = ""              # the human words we matched
    metric_column: Optional[str] = None
    region: Optional[str] = None
    timeframe: str = ""
    actual_value: Optional[float] = None
    difference_percent: Optional[float] = None
    status: str = STATUS_WARN
    source: str = ""
    reason: str = ""                   # why WARNING / what we compared
    context: str = ""                  # full original sentence
    sql_query: str = ""                # for "Copy Source Query"
    sql_params: Tuple[Any, ...] = ()
    db_row_id: Optional[int] = None    # for "Correct the Claim in Database"
    db_row: Dict[str, Any] = field(default_factory=dict)
    interpretation: str = ""           # "literal" | "x1000" | "/1000"
    #: Set when the metric was resolved by evaluating a derived-metric formula
    #: rather than reading a single row.
    formula: str = ""
    formula_working: str = ""          # "1,750,000 + 3,050,000 = 4,800,000"
    #: Populated when validation rules flagged the value (e.g. a 3800% rate).
    validation_note: str = ""
    #: Set when smart-suggest proposed a metric name for an unknown term.
    suggested_metric: str = ""
    #: True when the number's format was genuinely ambiguous ("1,234").
    ambiguous_format: bool = False

    # -- presentation helpers ---------------------------------------------
    def as_tuple(self) -> Tuple[str, ...]:
        """The exact column order used by the Treeview and the CSV export."""
        return (
            self.filename,
            str(self.line_no) if self.line_no else "-",
            _fmt(self.claimed_value) if self.claimed_value is not None else self.claimed_display,
            self.metric_column or self.metric_text or "(unknown)",
            self.timeframe or "-",
            _fmt(self.actual_value) if self.actual_value is not None else "-",
            self.status,
            self.source or "-",
        )

    def to_dict(self) -> Dict[str, Any]:
        """Flat dictionary used by the CSV / JSON / Excel exporters."""
        return {
            "Filename": self.filename,
            "Line/Row": self.line_no,
            "Claimed Value": self.claimed_value,
            "Claimed (as written)": self.claimed_display,
            "Metric": self.metric_column or self.metric_text,
            "Metric Text": self.metric_text,
            "Region": self.region or "",
            "Timeframe": self.timeframe,
            "Actual Value": self.actual_value,
            "Difference %": (round(self.difference_percent, 4)
                             if self.difference_percent is not None else None),
            "Status": self.status,
            "Source": self.source,
            "Interpretation": self.interpretation,
            "Formula": self.formula,
            "Formula Working": self.formula_working,
            "Validation Note": self.validation_note,
            "Suggested Metric": self.suggested_metric,
            "Ambiguous Format": self.ambiguous_format,
            "Reason": self.reason,
            "Context": self.context,
            "SQL Query": self.rendered_query(),
        }

    def rendered_query(self) -> str:
        """The SQL with parameters substituted - copyable and runnable.

        We only ever do this for DISPLAY. The real query always used bound
        parameters, so there is no SQL-injection risk in the execution path.
        """
        if not self.sql_query:
            return ""
        rendered = self.sql_query
        for param in self.sql_params:
            literal = f"'{param}'" if isinstance(param, str) else str(param)
            rendered = rendered.replace("?", literal, 1)
            rendered = rendered.replace("%s", literal, 1)
        return " ".join(rendered.split())


class ValidationRules:
    """Sanity checks applied to a claim independently of the database.

    These catch mistakes the database cannot: a churn rate of 3800% is wrong
    even if no row exists to contradict it, and a negative user count is
    nonsense whatever the warehouse says. Rules come from the
    ``validation_rules`` block of config.json so users can tune them without
    touching code.

    Every method is total - malformed rules are ignored rather than raising.
    """

    def __init__(self, rules: Optional[Dict[str, Any]] = None) -> None:
        rules = rules if isinstance(rules, dict) else {}
        self.enabled = bool(rules.get("enabled", True))
        self.ranges: Dict[str, Dict[str, Any]] = (
            rules.get("ranges", {}) if isinstance(rules.get("ranges"), dict) else {})
        self.non_negative = {
            str(m).strip().lower()
            for m in (rules.get("non_negative", []) or [])
            if str(m).strip()
        }
        self.percent_suffixes = [
            str(s).lower() for s in (rules.get("percent_metric_suffixes", []) or [])
        ]
        self.on_violation = str(rules.get("on_violation", "warning")).strip().lower()

    def looks_like_percent(self, metric: str) -> bool:
        low = str(metric or "").lower()
        return any(low.endswith(s) or s in low for s in self.percent_suffixes)

    def check(self, metric: Optional[str], value: Optional[float],
              is_percent: bool = False) -> Optional[str]:
        """Return a human message if the value breaks a rule, else ``None``."""
        if not self.enabled or metric is None or value is None:
            return None

        key = str(metric).strip().lower()

        # --- explicit per-metric range ---------------------------------
        rule = self.ranges.get(key)
        if isinstance(rule, dict):
            try:
                low = rule.get("min")
                high = rule.get("max")
                if low is not None and value < float(low):
                    return (f"{metric} of {_fmt(value)} is below the configured "
                            f"minimum of {low}.")
                if high is not None and value > float(high):
                    return (f"{metric} of {_fmt(value)} exceeds the configured "
                            f"maximum of {high}. Check for a unit mix-up.")
            except (TypeError, ValueError):
                return None  # malformed rule - ignore rather than crash

        # --- implicit percentage sanity --------------------------------
        if (is_percent or self.looks_like_percent(key)) and key not in self.ranges:
            if value < -100.0 or value > 1000.0:
                return (f"{metric} of {_fmt(value)}% is outside a plausible "
                        f"percentage range.")

        # --- non-negative metrics --------------------------------------
        if key in self.non_negative and value < 0:
            return f"{metric} cannot be negative, but the claim says {_fmt(value)}."

        return None


class Verifier:
    """Compares claims against the data sources and produces verdicts."""

    def __init__(self, data: DataSourceManager, tolerance_percent: float = 1.0,
                 default_to_latest: bool = True,
                 formula_engine: Optional[FormulaEngine] = None,
                 validation: Optional[ValidationRules] = None) -> None:
        self.data = data
        self.tolerance = float(tolerance_percent)
        self.default_to_latest = bool(default_to_latest)
        #: Derived-metric evaluator. An empty engine means no formulas.
        self.formulas = formula_engine if formula_engine is not None else FormulaEngine()
        #: Range / sanity checks.
        self.validation = validation if validation is not None else ValidationRules()
        # Cached so we do not hit the DB for every single claim.
        self._latest_period: Optional[Tuple[str, int]] = None

    # -- helpers -----------------------------------------------------------
    def latest_period(self) -> Optional[Tuple[str, int]]:
        if self._latest_period is None:
            self._latest_period = self.data.latest_period()
        return self._latest_period

    @staticmethod
    def _percent_difference(claimed: float, actual: float) -> float:
        """Percentage difference relative to the ACTUAL (truth) value.

        Using the truth as the denominator is the accounting convention: a
        claim of 110 against a truth of 100 is "10% too high", not "9.1%".
        When the truth is exactly zero we fall back to an absolute comparison
        to avoid dividing by zero.
        """
        if actual == 0:
            return 0.0 if claimed == 0 else 100.0
        return abs(claimed - actual) / abs(actual) * 100.0

    def _candidate_values(self, claim: Claim) -> List[Tuple[float, str]]:
        """Every reasonable numeric reading of the same claim.

        See the module docstring for why this exists. Order matters: the first
        interpretation that passes is the one we report, so we list the most
        literal reading first.
        """
        base = claim.value
        candidates: List[Tuple[float, str]] = [(base, "literal")]

        if claim.scale != 1.0:
            candidates.append((base * claim.scale, f"x{int(claim.scale)}"))

        # INTERNATIONAL FORMATS: when the token was genuinely ambiguous
        # ("1,234" could be 1234 or 1.234 depending on locale) the number
        # normalizer handed us the other reading. Try it before failing, so a
        # locale difference can never produce a false RED.
        for alternative in getattr(claim, "alternative_values", []) or []:
            candidates.append((alternative, "locale-alternative"))

        # A claim written as a plain big number may be matching a DB value that
        # is stored in thousands (or vice versa).
        for factor, label in ((1_000.0, "x1000"), (1_000_000.0, "x1000000"),
                              (0.001, "/1000"), (0.000001, "/1000000")):
            candidates.append((base * factor, label))

        # De-duplicate while preserving order.
        seen: set = set()
        unique: List[Tuple[float, str]] = []
        for value, label in candidates:
            key = round(value, 6)
            if key not in seen:
                seen.add(key)
                unique.append((value, label))
        return unique

    # -- the main entry point ----------------------------------------------
    def verify_claim(self, claim: Claim, filename: str = "(pasted text)") -> VerificationResult:
        """Check one claim and return a fully populated result row."""
        result = VerificationResult(
            filename=filename,
            line_no=claim.line_no,
            claimed_display=claim.raw_value,
            claimed_value=claim.value,
            metric_text=claim.metric_text,
            metric_column=claim.metric_column,
            region=claim.region,
            context=claim.context,
        )

        # --- YELLOW #1: we never identified what the number refers to ------
        if not claim.metric_column:
            result.status = STATUS_WARN
            result.timeframe = claim.timeframe.describe()
            result.reason = (
                f"Could not map '{claim.metric_text or 'this number'}' to a known "
                f"metric. Add a keyword to METRIC_KEYWORD_MAP in nlp_engine.py."
            )
            result.source = "-"
            return result

        # --- resolve the timeframe, defaulting when the text was silent ----
        quarter = claim.timeframe.quarter
        year = claim.timeframe.year
        if quarter is None and year is None and self.default_to_latest:
            latest = self.latest_period()
            if latest:
                quarter, year = latest
                claim.timeframe.quarter, claim.timeframe.year = latest
                claim.timeframe.inferred = True
        result.timeframe = claim.timeframe.describe()
        result.ambiguous_format = bool(getattr(claim, "ambiguous_format", False))

        # --- DERIVED METRIC? evaluate the formula instead of reading a row --
        # A metric like 'total_revenue' has no row of its own; it is the sum of
        # its constituents. We resolve each constituent for THIS claim's
        # region/period, then compare against the computed total.
        if self.formulas.has(claim.metric_column):
            return self._verify_against_formula(claim, result, quarter, year)

        # --- ask the data layer for the truth ------------------------------
        lookup: LookupResult = self.data.lookup(
            metric=claim.metric_column,
            quarter=quarter,
            year=year,
            region=claim.region,
        )
        result.sql_query = lookup.query
        result.sql_params = lookup.params

        # --- YELLOW #2: the metric/period/region simply is not in any DB ---
        if not lookup.found:
            result.status = STATUS_WARN
            result.reason = (
                f"No data for metric '{claim.metric_column}'"
                + (f", region '{claim.region}'" if claim.region else "")
                + (f", {result.timeframe}" if result.timeframe else "")
                + f". {lookup.note}"
            )
            result.source = "not found"
            return result

        actual = float(lookup.value or 0.0)
        result.actual_value = actual
        result.source = lookup.source
        result.db_row = lookup.row
        result.db_row_id = lookup.row.get("id")

        # --- compare, trying each numeric interpretation -------------------
        # IMPORTANT NUANCE: alternative scalings exist only to RESCUE a claim
        # that is really correct but written in shorthand ("4.8M" vs 4800000).
        # They must never be used to *describe* a failure - reporting "you
        # claimed 0.0000048" when the user wrote "4.8" would be nonsense. So a
        # non-literal interpretation is only adopted when it actually passes;
        # otherwise we report the natural reading of what the user wrote.
        natural_value = claim.value * (claim.scale if claim.scale != 1.0 else 1.0)
        natural_diff = self._percent_difference(natural_value, actual)

        passing_value: Optional[float] = None
        passing_label = ""
        passing_diff: Optional[float] = None

        for candidate, label in self._candidate_values(claim):
            diff = self._percent_difference(candidate, actual)
            if diff < self.tolerance:
                passing_value, passing_label, passing_diff = candidate, label, diff
                break

        if passing_value is not None:
            result.status = STATUS_PASS
            result.claimed_value = passing_value
            result.difference_percent = passing_diff
            result.interpretation = passing_label
            result.reason = (
                f"Within tolerance ({passing_diff:.3f}% < {self.tolerance}%)"
                + ("" if passing_label in ("literal", "")
                   else f", reading the claim as '{passing_label}' "
                        f"({_fmt(claim.value)} -> {_fmt(passing_value)})")
                + (f". {lookup.note}" if lookup.note else "")
            )
        else:
            result.status = STATUS_FAIL
            result.claimed_value = natural_value
            result.difference_percent = natural_diff
            result.interpretation = "literal" if claim.scale == 1.0 else f"x{int(claim.scale)}"
            result.reason = (
                f"Claimed {_fmt(natural_value)} but the database says "
                f"{_fmt(actual)} - off by {natural_diff:.2f}% "
                f"(tolerance {self.tolerance}%). No alternative reading of the "
                f"number matches either. Source row: {lookup.source}."
                + (f" {lookup.note}" if lookup.note else "")
            )

        # --- validation rules run regardless of the verdict ----------------
        self._apply_validation(claim, result)
        return result

    # -- derived metrics ----------------------------------------------------
    def _verify_against_formula(self, claim: Claim, result: VerificationResult,
                                quarter: Optional[str],
                                year: Optional[int]) -> VerificationResult:
        """Compare a claim against a CALCULATED value rather than a stored row.

        Each constituent metric is looked up for the same region/period as the
        claim. If any constituent is missing we return AMBER naming exactly
        which one - never a misleading RED, because we genuinely do not know
        what the total should be.
        """
        metric = str(claim.metric_column)
        result.formula = self.formulas.expression_for(metric)

        # Records the SQL of the last constituent so "Copy Source Query" still
        # gives the user something runnable; the full provenance of every
        # constituent goes into the reason text and the export.
        captured: Dict[str, str] = {}

        def resolver(name: str) -> Tuple[Optional[float], str]:
            """Fetch one constituent for this claim's region and period."""
            lookup = self.data.lookup(metric=name, quarter=quarter, year=year,
                                      region=claim.region)
            if lookup.found:
                captured[name] = lookup.query
                if not result.sql_query:
                    result.sql_query = lookup.query
                    result.sql_params = lookup.params
                return lookup.value, lookup.source
            return None, lookup.note or "not found"

        outcome: FormulaResult = self.formulas.evaluate(metric, resolver)

        # --- AMBER: a constituent is missing or the formula is broken -------
        if not outcome.ok or outcome.value is None:
            result.status = STATUS_WARN
            result.source = "formula (incomplete)"
            result.formula_working = outcome.describe()
            result.reason = (
                f"'{metric}' is a derived metric defined as "
                f"[{result.formula}]. {outcome.error} "
                f"Add the missing metric(s) to the database, or remove the "
                f"formula from config.json."
            )
            self._apply_validation(claim, result)
            return result

        actual = float(outcome.value)
        result.actual_value = actual
        result.source = f"formula: {result.formula}"
        result.formula_working = outcome.describe()

        natural_value = claim.value * (claim.scale if claim.scale != 1.0 else 1.0)
        natural_diff = self._percent_difference(natural_value, actual)

        passing_value: Optional[float] = None
        passing_label = ""
        passing_diff: Optional[float] = None
        for candidate, label in self._candidate_values(claim):
            diff = self._percent_difference(candidate, actual)
            if diff < self.tolerance:
                passing_value, passing_label, passing_diff = candidate, label, diff
                break

        provenance = "; ".join(f"{k}={_fmt(v)} [{outcome.sources.get(k, '?')}]"
                               for k, v in sorted(outcome.resolved.items()))

        if passing_value is not None:
            result.status = STATUS_PASS
            result.claimed_value = passing_value
            result.difference_percent = passing_diff
            result.interpretation = passing_label
            result.reason = (
                f"Matches the calculated value ({passing_diff:.3f}% < "
                f"{self.tolerance}%). {outcome.describe()}. Constituents: {provenance}."
            )
        else:
            result.status = STATUS_FAIL
            result.claimed_value = natural_value
            result.difference_percent = natural_diff
            result.interpretation = (
                "literal" if claim.scale == 1.0 else f"x{int(claim.scale)}")
            result.reason = (
                f"Claimed {_fmt(natural_value)} but the formula computes "
                f"{_fmt(actual)} - off by {natural_diff:.2f}%. "
                f"{outcome.describe()}. Constituents: {provenance}."
            )

        self._apply_validation(claim, result)
        return result

    # -- validation ---------------------------------------------------------
    def _apply_validation(self, claim: Claim, result: VerificationResult) -> None:
        """Attach a range/sanity note and escalate the verdict if configured.

        Runs on every result, including PASSes: a value can agree with the
        database and still be implausible, which usually means BOTH are wrong.
        """
        try:
            message = self.validation.check(
                result.metric_column or claim.metric_column,
                result.claimed_value if result.claimed_value is not None else claim.value,
                is_percent=claim.is_percent,
            )
        except Exception:
            return  # validation must never break a run

        if not message:
            return

        result.validation_note = message
        if self.validation.on_violation == "fail":
            result.status = STATUS_FAIL
        elif result.status == STATUS_PASS:
            # Agreeing with the database but implausible - worth a flag.
            result.status = STATUS_WARN
        result.reason = f"{result.reason} VALIDATION: {message}".strip()

    def verify_many(self, claims: List[Claim], filename: str = "(pasted text)",
                    progress=None) -> List[VerificationResult]:
        """Verify a list of claims, optionally reporting progress.

        ``progress`` is called as ``progress(done, total)`` after each claim so
        the GUI's status bar and progress bar can update in real time from the
        worker thread.
        """
        results: List[VerificationResult] = []
        total = len(claims)
        for index, claim in enumerate(claims, start=1):
            results.append(self.verify_claim(claim, filename=filename))
            if progress is not None:
                progress(index, total)
        return results

    @staticmethod
    def empty_result(filename: str, reason: str) -> VerificationResult:
        """A YELLOW placeholder for a file/text in which no numbers were found."""
        return VerificationResult(
            filename=filename,
            status=STATUS_WARN,
            reason=reason,
            claimed_display="-",
            metric_text="(none)",
            source="-",
        )


def summarise(results: List[VerificationResult]) -> Dict[str, int]:
    """Count verdicts - shown in the results log and the status bar."""
    tally = {STATUS_PASS: 0, STATUS_FAIL: 0, STATUS_WARN: 0}
    for item in results:
        tally[item.status] = tally.get(item.status, 0) + 1
    tally["TOTAL"] = len(results)
    return tally


def _fmt(value: Optional[float]) -> str:
    """Format numbers for humans: 4800000.0 -> '4,800,000', 2.3 -> '2.3'."""
    if value is None:
        return "-"
    if abs(value - round(value)) < 1e-9 and abs(value) >= 1000:
        return f"{value:,.0f}"
    if abs(value) >= 1000:
        return f"{value:,.2f}"
    formatted = f"{value:,.4f}".rstrip("0").rstrip(".")
    return formatted or "0"
