"""
numbers.py
==========

GLOBAL NUMBER NORMALIZER
------------------------
Replaces the old US-centric parser (which just did ``s.replace(",", "")``) with
one that reads numbers the way the rest of the world actually writes them:

    US / UK      1,234,567.89     comma groups, dot decimal
    European     1.234.567,89     dot groups, comma decimal
    Swiss        1'234'567.89     apostrophe groups, dot decimal
    Indian        12,34,567.89    2-digit groups after the first 3
    SI / French   1 234 567,89    space (or NBSP) groups
    Bare          1234567.89

THE HARD PART: GENUINE AMBIGUITY
--------------------------------
``1,234`` is 1234 in the US and 1.234 in Germany. ``1.234`` is the reverse.
No parser can resolve this from the token alone - the information is simply not
there. So we work in two layers:

  1. :func:`parse_number` classifies a single token and returns a
     :class:`ParsedNumber` carrying BOTH the chosen value and an
     ``ambiguous`` flag plus the alternative reading.
  2. :func:`detect_document_profile` scans the WHOLE document first, counts
     unambiguous evidence (any token with two different separators, or a
     3+ digit group after a comma, settles it), and returns a
     :class:`NumberProfile`. Passing that profile back into
     :func:`parse_number` resolves the ambiguous cases consistently.

This mirrors how a human reads a report: you work out the convention once from
context, then apply it throughout.

WHY THE VERIFIER STILL CANNOT BE WRONG
--------------------------------------
When a token is irreducibly ambiguous we hand the alternative reading to the
verifier via ``alternatives``. The verifier already tries multiple numeric
interpretations before failing a claim, so an ambiguous ``1,234`` will PASS
against either 1234 or 1.234 - and the report records which reading matched.
Ambiguity therefore never produces a false FAIL.

HOW TO EXTEND
-------------
Add a separator character to :data:`GROUP_SEPARATORS` and, if it can also act
as a decimal point, to :data:`DECIMAL_SEPARATORS`. Everything else adapts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Character inventory
# ---------------------------------------------------------------------------
# Characters that can group thousands. NBSP and narrow-NBSP are included
# because Excel and Word emit them constantly.
GROUP_SEPARATORS = {",", ".", "'", "\u00a0", "\u202f", "\u2009", " ", "\u2019"}

# Characters that can act as a decimal point.
DECIMAL_SEPARATORS = {",", "."}

# Currency symbols stripped before parsing.
CURRENCY_SYMBOLS = "$€£¥₹₽₩¢₪₺R\u20ac"

# Scale suffixes. These apply regardless of which delimiter style was used, so
# "1.234,5 Mio" and "1,234.5M" both scale by a million.
SCALE_SUFFIX_MAP: Dict[str, float] = {
    "k": 1e3, "K": 1e3, "thousand": 1e3, "tsd": 1e3, "mil": 1e3,
    "m": 1e6, "mm": 1e6, "mio": 1e6, "million": 1e6, "millions": 1e6,
    "b": 1e9, "bn": 1e9, "billion": 1e9, "billions": 1e9, "mrd": 1e9,
    "t": 1e12, "tn": 1e12, "trillion": 1e12,
    # Indian numbering, which is a scale system rather than a grouping style.
    "lakh": 1e5, "lac": 1e5, "lakhs": 1e5,
    "crore": 1e7, "cr": 1e7, "crores": 1e7,
}

#: Profiles the detector can conclude.
PROFILE_US = "us"            # 1,234,567.89
PROFILE_EU = "eu"            # 1.234.567,89
PROFILE_SWISS = "swiss"      # 1'234'567.89
PROFILE_INDIAN = "indian"    # 12,34,567.89
PROFILE_SPACE = "space"      # 1 234 567,89
PROFILE_UNKNOWN = "unknown"

# Indian grouping: last group of 3, preceding groups of 2.
INDIAN_GROUP_RE = re.compile(r"^\d{1,2}(?:,\d{2})+,\d{3}$")
# Western grouping: all groups of exactly 3.
WESTERN_GROUP_RE = re.compile(r"^\d{1,3}(?:([,.\u00a0\u202f\u2009' ])\d{3})+$")


@dataclass
class ParsedNumber:
    """The result of normalizing one numeric token."""

    value: float                       # best-guess value, scale NOT applied
    raw: str = ""                      # the original token
    profile: str = PROFILE_UNKNOWN     # which convention we concluded
    ambiguous: bool = False            # True when another reading is defensible
    alternatives: List[float] = field(default_factory=list)
    scale: float = 1.0                 # from a k/M/B/lakh/crore suffix
    is_percent: bool = False
    is_currency: bool = False
    negative_from_parens: bool = False

    def scaled(self) -> float:
        """Value with its scale suffix applied."""
        return self.value * self.scale

    def all_readings(self) -> List[float]:
        """Every defensible value, best first - fed to the verifier."""
        out = [self.scaled()]
        for alt in self.alternatives:
            candidate = alt * self.scale
            if all(abs(candidate - existing) > 1e-12 for existing in out):
                out.append(candidate)
        return out


@dataclass
class NumberProfile:
    """A document-wide conclusion about which number convention is in use."""

    name: str = PROFILE_UNKNOWN
    decimal: str = "."
    group: str = ","
    confidence: float = 0.0
    evidence: Dict[str, int] = field(default_factory=dict)

    def describe(self) -> str:
        if self.name == PROFILE_UNKNOWN:
            return "unknown (assuming US/UK)"
        return (f"{self.name} (decimal '{self.decimal}', group '{self.group}', "
                f"confidence {self.confidence:.0%})")


def _strip_decorations(token: str) -> Tuple[str, bool, bool, bool]:
    """Remove currency, percent and parentheses. Returns the bare digit string.

    Returns ``(core, is_currency, is_percent, parenthesised)``.
    """
    text = token.strip()
    parenthesised = text.startswith("(") and text.endswith(")")
    if parenthesised:
        text = text[1:-1].strip()

    is_currency = any(symbol in text for symbol in CURRENCY_SYMBOLS)
    for symbol in CURRENCY_SYMBOLS:
        text = text.replace(symbol, "")

    is_percent = "%" in text
    text = text.replace("%", "")

    return text.strip(), is_currency, is_percent, parenthesised


def _split_scale(text: str) -> Tuple[str, float]:
    """Peel a trailing scale word/letter off the token.

    ``"4.8M"`` -> ``("4.8", 1e6)``; ``"12 crore"`` -> ``("12", 1e7)``.
    """
    stripped = text.strip()
    # Longest suffixes first so "mio" is not shadowed by "m".
    for suffix in sorted(SCALE_SUFFIX_MAP, key=len, reverse=True):
        pattern = re.compile(r"(.*?)[\s\u00a0]*" + re.escape(suffix) + r"\.?$",
                             re.IGNORECASE)
        match = pattern.match(stripped)
        if match and match.group(1).strip():
            head = match.group(1).strip()
            # Only accept if what remains actually looks numeric.
            if re.search(r"\d$", head):
                return head, SCALE_SUFFIX_MAP[suffix]
    return stripped, 1.0


def _classify_separators(core: str) -> Tuple[Optional[str], Optional[str], str, bool, List[float]]:
    """Work out which characters are grouping and which is the decimal point.

    Returns ``(decimal_sep, group_sep, profile, ambiguous, alternatives)``.
    This is the core decision procedure; each branch is commented with the
    reasoning a human would use.
    """
    separators = [ch for ch in core if ch in GROUP_SEPARATORS]
    unique = []
    for ch in separators:
        if ch not in unique:
            unique.append(ch)

    # ---- Case 0: no separators at all -> unambiguous -------------------
    if not unique:
        return None, None, PROFILE_UNKNOWN, False, []

    # ---- Case 1: two DIFFERENT separators -> fully determined ----------
    # The one that appears LAST is the decimal point; the other groups.
    # "1.234.567,89" -> ',' last  -> decimal ','  group '.'  (European)
    # "1,234,567.89" -> '.' last  -> decimal '.'  group ','  (US)
    # "1'234'567.89" -> '.' last  -> decimal '.'  group "'"  (Swiss)
    if len(unique) >= 2:
        last_positions = {ch: core.rfind(ch) for ch in unique}
        decimal_sep = max(last_positions, key=lambda c: last_positions[c])
        group_candidates = [c for c in unique if c != decimal_sep]
        group_sep = group_candidates[-1] if group_candidates else None

        # A decimal separator must appear exactly once and be followed by digits.
        if core.count(decimal_sep) != 1 or decimal_sep not in DECIMAL_SEPARATORS:
            # e.g. "1'234'567" with an apostrophe last - it is still grouping.
            return None, unique[0], _profile_for(None, unique[0]), False, []

        profile = _profile_for(decimal_sep, group_sep)
        # Indian grouping check on the integer part.
        integer_part = core.split(decimal_sep)[0]
        if group_sep == "," and INDIAN_GROUP_RE.match(integer_part):
            profile = PROFILE_INDIAN
        return decimal_sep, group_sep, profile, False, []

    # ---- Case 2: exactly ONE kind of separator -> may be ambiguous -----
    sep = unique[0]
    count = core.count(sep)
    tail = core.rsplit(sep, 1)[1] if sep in core else ""

    # 2a. Apostrophe/space/NBSP are NEVER decimal points -> pure grouping.
    if sep not in DECIMAL_SEPARATORS:
        return None, sep, _profile_for(None, sep), False, []

    # 2b. Appears more than once -> must be grouping ("1.234.567").
    if count > 1:
        # Indian style uses commas in a 2-2-3 pattern.
        if sep == "," and INDIAN_GROUP_RE.match(core):
            return None, ",", PROFILE_INDIAN, False, []
        return None, sep, _profile_for(None, sep), False, []

    # 2c. Appears once. Decide by the length of the trailing group.
    if len(tail) == 3 and tail.isdigit():
        # "1,234" or "1.234" - THE classic ambiguity. Both readings are valid.
        head = core.split(sep)[0]
        as_group = float(head + tail)              # 1234
        as_decimal = float(f"{head}.{tail}")       # 1.234
        # Default: treat as grouping (matches US/UK, the commonest in reports),
        # but hand back the alternative so nothing is ever falsely failed.
        return (None, sep, _profile_for(None, sep), True, [as_decimal]) \
            if True else (None, None, PROFILE_UNKNOWN, True, [as_group])

    # 2d. Trailing group is not 3 digits -> it is a decimal ("1,5" / "12.75").
    return sep, None, _profile_for(sep, None), False, []


def _profile_for(decimal_sep: Optional[str], group_sep: Optional[str]) -> str:
    """Name the convention implied by a (decimal, group) separator pair."""
    if group_sep == "'" or group_sep == "\u2019":
        return PROFILE_SWISS
    if group_sep in ("\u00a0", "\u202f", "\u2009", " "):
        return PROFILE_SPACE
    if decimal_sep == "," or group_sep == ".":
        return PROFILE_EU
    if decimal_sep == "." or group_sep == ",":
        return PROFILE_US
    return PROFILE_UNKNOWN


def parse_number(token: str,
                 profile: Optional[NumberProfile] = None) -> Optional[ParsedNumber]:
    """Normalize one numeric token into a :class:`ParsedNumber`.

    ``profile`` is the document-wide convention from
    :func:`detect_document_profile`. When supplied it resolves the ambiguous
    "one separator, three trailing digits" case; without it we default to the
    US/UK reading and expose the alternative.

    Returns ``None`` if the token holds no digits at all.
    """
    if token is None:
        return None

    core, is_currency, is_percent, parenthesised = _strip_decorations(str(token))
    core, scale = _split_scale(core)

    negative = core.startswith("-") or core.startswith("\u2212")
    core = core.lstrip("-\u2212+").strip()

    if not re.search(r"\d", core):
        return None
    # Reject tokens carrying stray letters (e.g. "12abc") - a scale suffix has
    # already been peeled off by this point, so anything left is junk.
    if re.search(r"[A-Za-z]", core):
        return None

    decimal_sep, group_sep, detected, ambiguous, alternatives = _classify_separators(core)

    # --- let a confident document profile override an ambiguous token ----
    if ambiguous and profile is not None and profile.name != PROFILE_UNKNOWN:
        if profile.decimal == "," and group_sep == ",":
            # Document is European: "1,234" means 1.234
            decimal_sep, group_sep = ",", None
            ambiguous, alternatives = False, []
            detected = profile.name
        elif profile.decimal == "." and group_sep == ".":
            # Document is US: "1.234" means 1.234 (a decimal, not grouping)
            decimal_sep, group_sep = ".", None
            ambiguous, alternatives = False, []
            detected = profile.name
        elif profile.decimal == "." and group_sep == ",":
            ambiguous, alternatives = False, []
            detected = profile.name
        elif profile.decimal == "," and group_sep == ".":
            ambiguous, alternatives = False, []
            detected = profile.name

    # --- build the canonical machine-readable string ---------------------
    canonical = core
    if group_sep:
        canonical = canonical.replace(group_sep, "")
    if decimal_sep:
        canonical = canonical.replace(decimal_sep, ".")
    # Any leftover grouping characters (spaces, apostrophes) go away.
    for ch in GROUP_SEPARATORS:
        if ch != ".":
            canonical = canonical.replace(ch, "")
    # A stray trailing dot ("1.234." ) would break float().
    canonical = canonical.rstrip(".")

    try:
        value = float(canonical)
    except ValueError:
        return None

    if negative or parenthesised:
        value = -value
        alternatives = [-a for a in alternatives]

    return ParsedNumber(
        value=value,
        raw=str(token).strip(),
        profile=detected if detected != PROFILE_UNKNOWN
        else (profile.name if profile else PROFILE_UNKNOWN),
        ambiguous=ambiguous,
        alternatives=alternatives,
        scale=scale,
        is_percent=is_percent,
        is_currency=is_currency,
        negative_from_parens=parenthesised,
    )


# ---------------------------------------------------------------------------
# DOCUMENT-WIDE PROFILE DETECTION
# ---------------------------------------------------------------------------
# Any token matching one of these is UNAMBIGUOUS evidence for a convention.
_EVIDENCE_PATTERNS: List[Tuple[str, re.Pattern]] = [
    # 1.234,56  -> dot groups + comma decimal
    (PROFILE_EU, re.compile(r"\d{1,3}(?:\.\d{3})+,\d+")),
    # 1,234.56  -> comma groups + dot decimal
    (PROFILE_US, re.compile(r"\d{1,3}(?:,\d{3})+\.\d+")),
    # 1'234.56 or 1'234'567
    (PROFILE_SWISS, re.compile(r"\d{1,3}(?:['\u2019]\d{3})+")),
    # 12,34,567 -> Indian
    (PROFILE_INDIAN, re.compile(r"\d{1,2}(?:,\d{2})+,\d{3}")),
    # 1 234,56 -> space groups
    (PROFILE_SPACE, re.compile(r"\d{1,3}(?:[\u00a0\u202f\u2009 ]\d{3})+,\d+")),
    # Multiple dot groups with no comma at all: 1.234.567
    (PROFILE_EU, re.compile(r"\d{1,3}(?:\.\d{3}){2,}")),
    # Multiple comma groups with no dot: 1,234,567
    (PROFILE_US, re.compile(r"\d{1,3}(?:,\d{3}){2,}")),
    # A comma followed by 1, 2 or 4+ digits can only be a decimal comma: "3,5"
    (PROFILE_EU, re.compile(r"\d,\d{1,2}(?!\d)")),
    # A dot followed by 1, 2 or 4+ digits can only be a decimal point: "3.5"
    (PROFILE_US, re.compile(r"\d\.\d{1,2}(?!\d)")),
]

_PROFILE_SEPARATORS: Dict[str, Tuple[str, str]] = {
    PROFILE_US: (".", ","),
    PROFILE_EU: (",", "."),
    PROFILE_SWISS: (".", "'"),
    PROFILE_INDIAN: (".", ","),
    PROFILE_SPACE: (",", "\u00a0"),
}


def detect_document_profile(text: str) -> NumberProfile:
    """Infer the numeric convention used across a whole document.

    Counts unambiguous evidence for each style and picks the winner. Tokens
    like ``1,234`` that are compatible with several styles contribute nothing,
    which is exactly right - they carry no information.

    Call this ONCE per file, then pass the result to every
    :func:`parse_number` call for that file.
    """
    if not text:
        return NumberProfile()

    sample = text[:200_000]  # cap the work on huge files
    tally: Dict[str, int] = {}

    for name, pattern in _EVIDENCE_PATTERNS:
        hits = len(pattern.findall(sample))
        if hits:
            tally[name] = tally.get(name, 0) + hits

    if not tally:
        return NumberProfile(evidence={})

    # Swiss and Indian are highly specific markers - weight them up so a single
    # "12,34,567" is not drowned out by ordinary "3.5"s elsewhere.
    weighted = dict(tally)
    for special in (PROFILE_SWISS, PROFILE_INDIAN):
        if special in weighted:
            weighted[special] *= 5

    winner = max(weighted, key=lambda k: weighted[k])
    total = sum(weighted.values()) or 1
    confidence = weighted[winner] / total

    decimal_sep, group_sep = _PROFILE_SEPARATORS.get(winner, (".", ","))
    return NumberProfile(name=winner, decimal=decimal_sep, group=group_sep,
                         confidence=confidence, evidence=tally)


def format_number(value: Optional[float], profile: Optional[NumberProfile] = None) -> str:
    """Render a float back out for display, honouring the document profile.

    Used by the GUI and the exporters so a European document's report reads
    naturally to a European user.
    """
    if value is None:
        return "-"

    if abs(value - round(value)) < 1e-9 and abs(value) >= 1000:
        base = f"{value:,.0f}"
    elif abs(value) >= 1000:
        base = f"{value:,.2f}"
    else:
        base = f"{value:,.4f}".rstrip("0").rstrip(".") or "0"

    if profile is None or profile.name in (PROFILE_UNKNOWN, PROFILE_US, PROFILE_INDIAN):
        return base

    # Swap the Python default (US) separators for the document's.
    decimal_sep, group_sep = _PROFILE_SEPARATORS.get(profile.name, (".", ","))
    return (base.replace(",", "\x00").replace(".", decimal_sep)
                .replace("\x00", group_sep))
