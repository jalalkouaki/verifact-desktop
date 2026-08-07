"""
suggestions.py
==============

SMART-SUGGEST FOR UNKNOWN METRICS
---------------------------------
When a claim goes AMBER because its metric name is in neither the database nor
the keyword map, we do not just give up. We fuzzy-match the unknown text
against every metric name we DO know and offer the user a one-click fix:

    "Did you mean 'active_users'?"   [Yes]  [No]  [Ignore]

The answer is remembered forever in a local SQLite table (``metric_aliases``),
so the next run - and every future session - resolves that text automatically
without asking again.

MATCHING STRATEGY (best available, degrades gracefully)
-------------------------------------------------------
1. ``Levenshtein.ratio`` if the optional ``python-Levenshtein`` package is
   installed - it is a C extension and roughly 10x faster on large candidate
   lists.
2. Otherwise ``difflib.SequenceMatcher`` from the standard library, which is
   always present. Results are near-identical for short metric names.

On top of raw string similarity we add three domain-aware boosts, because
plain edit distance handles business vocabulary poorly:

  * TOKEN OVERLAP - "monthly active user count" vs "active_users" share two
    tokens; edit distance alone scores that pair badly.
  * CONTAINMENT   - one name being a substring of the other is strong evidence.
  * ACRONYM       - "MAU" matching "monthly_active_users" by initials.

THREE-STATE MEMORY
------------------
Each decision is stored with a verdict:
    accepted -> always rewrite this text to the chosen metric
    rejected -> never suggest that particular pairing again
    ignored  -> never prompt for this text again at all

That distinction matters: "No" means *wrong suggestion, keep trying*, while
"Ignore" means *stop bothering me about this string*.

STABILITY
---------
Every database call is wrapped. If the alias table cannot be created or read,
suggestions silently degrade to "no memory" rather than breaking verification.
"""

from __future__ import annotations

import difflib
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# Optional C-accelerated string distance.
try:
    import Levenshtein  # type: ignore
    LEVENSHTEIN_AVAILABLE = True
except Exception:  # pragma: no cover
    Levenshtein = None  # type: ignore
    LEVENSHTEIN_AVAILABLE = False


# Verdict constants stored in the alias table.
VERDICT_ACCEPTED = "accepted"
VERDICT_REJECTED = "rejected"
VERDICT_IGNORED = "ignored"

#: Below this combined score we do not bother the user at all.
DEFAULT_THRESHOLD = 0.55


@dataclass
class Suggestion:
    """A candidate correction for an unrecognised metric name."""

    source_text: str
    suggested_metric: str
    score: float
    reason: str = ""

    def question(self) -> str:
        return (f"'{self.source_text}' is not a known metric.\n\n"
                f"Did you mean '{self.suggested_metric}'?\n\n"
                f"(match confidence {self.score:.0%} - {self.reason})")


def _tokenise(text: str) -> List[str]:
    """Split a metric name into comparable lowercase word tokens.

    ``"Monthly Active-Users (EU)"`` -> ``['monthly', 'active', 'users', 'eu']``
    """
    return [t for t in re.split(r"[^a-z0-9]+", str(text).lower()) if t]


def _string_ratio(a: str, b: str) -> float:
    """Similarity in [0,1] using the fastest available backend."""
    if not a or not b:
        return 0.0
    if LEVENSHTEIN_AVAILABLE:
        try:
            return float(Levenshtein.ratio(a, b))
        except Exception:
            pass
    return difflib.SequenceMatcher(None, a, b).ratio()


def _acronym_of(text: str) -> str:
    """First letter of each token: 'monthly_active_users' -> 'mau'."""
    return "".join(token[0] for token in _tokenise(text) if token)


def score_pair(source: str, candidate: str) -> Tuple[float, str]:
    """Score how likely ``candidate`` is the intended metric for ``source``.

    Returns ``(score, human_reason)``. The blend below was tuned against the
    kind of names business documents actually contain; adjust the weights here
    if your vocabulary behaves differently.
    """
    source_norm = " ".join(_tokenise(source))
    candidate_norm = " ".join(_tokenise(candidate))
    if not source_norm or not candidate_norm:
        return 0.0, ""

    # Exact match after normalisation - nothing beats this.
    if source_norm == candidate_norm:
        return 1.0, "exact match once punctuation is ignored"

    base = _string_ratio(source_norm, candidate_norm)
    reasons: List[str] = [f"{base:.0%} character similarity"]
    score = base

    source_tokens = set(_tokenise(source))
    candidate_tokens = set(_tokenise(candidate))

    # --- token overlap ---------------------------------------------------
    if source_tokens and candidate_tokens:
        shared = source_tokens & candidate_tokens
        if shared:
            overlap = len(shared) / min(len(source_tokens), len(candidate_tokens))
            score = max(score, 0.45 + 0.5 * overlap)
            reasons.append(f"shares the word{'s' if len(shared) > 1 else ''} "
                           f"{', '.join(sorted(shared))}")

    # --- containment -----------------------------------------------------
    if source_norm in candidate_norm or candidate_norm in source_norm:
        score = max(score, 0.80)
        reasons.append("one name contains the other")

    # --- acronym ---------------------------------------------------------
    compact_source = source_norm.replace(" ", "")
    if compact_source and compact_source == _acronym_of(candidate):
        score = max(score, 0.85)
        reasons.append(f"'{compact_source}' is the acronym of '{candidate}'")

    # --- singular/plural stem --------------------------------------------
    if source_norm.rstrip("s") == candidate_norm.rstrip("s"):
        score = max(score, 0.92)
        reasons.append("differs only by plural")

    return min(score, 1.0), "; ".join(reasons)


class AliasStore:
    """Persistent memory of the user's metric-naming decisions.

    Lives in the same SQLite file as the truth data, in its own table so it can
    never collide with ``financial_metrics``.
    """

    TABLE = "metric_aliases"

    def __init__(self, db_path: str) -> None:
        self.path = db_path
        self._lock = threading.Lock()
        self.available = False
        self.last_error = ""
        self._cache: Dict[str, Tuple[str, str]] = {}  # source -> (metric, verdict)
        self.initialise()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def initialise(self) -> None:
        """Create the alias table and warm the in-memory cache."""
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self.TABLE} (
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        source_text  TEXT NOT NULL,
                        metric_name  TEXT NOT NULL,
                        verdict      TEXT NOT NULL DEFAULT 'accepted',
                        created_at   TEXT NOT NULL,
                        hit_count    INTEGER NOT NULL DEFAULT 0,
                        UNIQUE(source_text, metric_name)
                    )
                    """
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{self.TABLE}_source "
                    f"ON {self.TABLE} (source_text)")
            self.available = True
            self.refresh()
        except sqlite3.Error as exc:
            self.available = False
            self.last_error = str(exc)

    def refresh(self) -> None:
        """Reload the cache so lookups never touch disk during a batch run."""
        if not self.available:
            return
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    f"SELECT source_text, metric_name, verdict FROM {self.TABLE}"
                ).fetchall()
            cache: Dict[str, Tuple[str, str]] = {}
            for row in rows:
                key = str(row["source_text"]).strip().lower()
                # An accepted mapping always wins over a rejection.
                if key in cache and cache[key][1] == VERDICT_ACCEPTED:
                    continue
                cache[key] = (row["metric_name"], row["verdict"])
            self._cache = cache
        except sqlite3.Error as exc:
            self.last_error = str(exc)

    # -- reads -------------------------------------------------------------
    def resolve(self, source_text: str) -> Optional[str]:
        """Return the accepted metric for this text, if the user taught us one."""
        entry = self._cache.get(str(source_text).strip().lower())
        if entry and entry[1] == VERDICT_ACCEPTED:
            return entry[0]
        return None

    def is_silenced(self, source_text: str) -> bool:
        """True when the user pressed Ignore for this text."""
        entry = self._cache.get(str(source_text).strip().lower())
        return bool(entry and entry[1] == VERDICT_IGNORED)

    def was_rejected(self, source_text: str, metric: str) -> bool:
        """True when the user already said No to this exact pairing."""
        if not self.available:
            return False
        try:
            with self._connect() as conn:
                row = conn.execute(
                    f"SELECT 1 FROM {self.TABLE} WHERE LOWER(source_text)=LOWER(?) "
                    f"AND LOWER(metric_name)=LOWER(?) AND verdict=?",
                    (source_text, metric, VERDICT_REJECTED)).fetchone()
            return row is not None
        except sqlite3.Error:
            return False

    def all_aliases(self) -> List[Dict[str, object]]:
        if not self.available:
            return []
        try:
            with self._connect() as conn:
                return [dict(r) for r in conn.execute(
                    f"SELECT id, source_text, metric_name, verdict, created_at, "
                    f"hit_count FROM {self.TABLE} ORDER BY source_text")]
        except sqlite3.Error:
            return []

    # -- writes ------------------------------------------------------------
    def remember(self, source_text: str, metric: str, verdict: str) -> bool:
        """Persist a decision. Safe to call repeatedly."""
        if not self.available:
            return False
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    f"""INSERT INTO {self.TABLE}
                        (source_text, metric_name, verdict, created_at, hit_count)
                        VALUES (?,?,?,?,0)
                        ON CONFLICT(source_text, metric_name)
                        DO UPDATE SET verdict=excluded.verdict""",
                    (str(source_text).strip(), str(metric).strip(), verdict,
                     datetime.now().isoformat(timespec="seconds")))
            self.refresh()
            return True
        except sqlite3.Error as exc:
            self.last_error = str(exc)
            return False

    def record_hit(self, source_text: str) -> None:
        """Count how often a learned alias saved the user a prompt."""
        if not self.available:
            return
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    f"UPDATE {self.TABLE} SET hit_count = hit_count + 1 "
                    f"WHERE LOWER(source_text)=LOWER(?) AND verdict=?",
                    (source_text, VERDICT_ACCEPTED))
        except sqlite3.Error:
            pass

    def forget(self, row_id: int) -> bool:
        if not self.available:
            return False
        try:
            with self._lock, self._connect() as conn:
                conn.execute(f"DELETE FROM {self.TABLE} WHERE id = ?", (int(row_id),))
            self.refresh()
            return True
        except sqlite3.Error:
            return False


class SuggestionEngine:
    """Finds the best candidate metric for an unrecognised name."""

    def __init__(self, store: AliasStore, threshold: float = DEFAULT_THRESHOLD) -> None:
        self.store = store
        self.threshold = float(threshold)
        self._candidates: List[str] = []

    @property
    def backend_name(self) -> str:
        return ("python-Levenshtein" if LEVENSHTEIN_AVAILABLE
                else "difflib (standard library)")

    def set_candidates(self, names: Iterable[str]) -> None:
        """Tell the engine which metric names exist (DB + keyword map + formulas)."""
        seen: List[str] = []
        for name in names:
            text = str(name).strip()
            if text and text not in seen:
                seen.append(text)
        self._candidates = seen

    @property
    def candidates(self) -> Sequence[str]:
        return tuple(self._candidates)

    def best_suggestion(self, source_text: str) -> Optional[Suggestion]:
        """Return the strongest candidate above the threshold, or ``None``.

        Respects the user's memory: silenced texts return nothing, and
        previously-rejected pairings are skipped so we never ask twice.
        """
        text = str(source_text or "").strip()
        if not text or not self._candidates:
            return None
        if self.store.is_silenced(text):
            return None

        ranked: List[Suggestion] = []
        for candidate in self._candidates:
            score, reason = score_pair(text, candidate)
            if score >= self.threshold and not self.store.was_rejected(text, candidate):
                ranked.append(Suggestion(text, candidate, score, reason))

        if not ranked:
            return None
        ranked.sort(key=lambda s: s.score, reverse=True)
        return ranked[0]

    def top_suggestions(self, source_text: str, limit: int = 5) -> List[Suggestion]:
        """All plausible candidates, best first - used by the chooser dialog."""
        text = str(source_text or "").strip()
        if not text:
            return []
        ranked = []
        for candidate in self._candidates:
            score, reason = score_pair(text, candidate)
            if score >= self.threshold * 0.7:
                ranked.append(Suggestion(text, candidate, score, reason))
        ranked.sort(key=lambda s: s.score, reverse=True)
        return ranked[:limit]
