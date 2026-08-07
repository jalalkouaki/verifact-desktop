"""
formulas.py
===========

DERIVED-METRIC FORMULA CHECKER
------------------------------
Lets a user declare metrics that are CALCULATED from other metrics, in
``config.json``::

    "derived_metrics": {
        "total_revenue": "region_a_revenue + region_b_revenue + apac_revenue",
        "gross_margin":  "(revenue - cost_of_sales) / revenue * 100",
        "arpu":          "revenue / active_users"
    }

When a claim's metric matches one of these keys, the verifier does NOT look for
a single database row. Instead it resolves every constituent metric for the
claim's region/timeframe, evaluates the expression, and compares the claim
against the computed result.

SAFETY IS THE WHOLE POINT
-------------------------
These expressions come from a text file a user edits by hand. Running them
through ``eval()`` would be a remote-code-execution hole - a config file could
contain ``__import__('os').system('...')``. So we never use ``eval``.

Instead we parse the expression to an AST with ``ast.parse(mode='eval')`` and
walk it against a STRICT WHITELIST of node types:

    numbers, names, + - * / // % **, unary -/+, parentheses, and a small set
    of safe functions (min, max, abs, round, sum).

Anything else - attribute access, subscripts, calls to unknown names, lambdas,
comprehensions, string literals - raises :class:`FormulaError` at validation
time. A malformed or malicious formula therefore fails loudly and safely,
producing an AMBER result rather than executing.

STABILITY CONTRACT
------------------
Nothing in this module raises to the caller except :class:`FormulaError`, and
:meth:`FormulaEngine.evaluate` catches even that, returning a structured
:class:`FormulaResult` with ``ok=False``. A bad formula can never crash a
verification run.
"""

from __future__ import annotations

import ast
import math
import operator
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


class FormulaError(Exception):
    """Raised for any unsafe, malformed or unresolvable formula."""


# ---------------------------------------------------------------------------
# The whitelist. Anything not listed here is rejected.
# ---------------------------------------------------------------------------
_ALLOWED_BINOPS: Dict[type, Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_ALLOWED_UNARYOPS: Dict[type, Callable[[float], float]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

# Safe, pure, side-effect-free helpers a finance user might reasonably want.
_ALLOWED_FUNCTIONS: Dict[str, Callable[..., float]] = {
    "min": min,
    "max": max,
    "abs": abs,
    "round": round,
    "sum": lambda *args: math.fsum(args),
    "avg": lambda *args: math.fsum(args) / len(args) if args else 0.0,
    "sqrt": math.sqrt,
}

# Node types permitted anywhere in the tree.
_ALLOWED_NODES: Tuple[type, ...] = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Name, ast.Load,
    ast.Call, ast.Constant,
) + tuple(_ALLOWED_BINOPS) + tuple(_ALLOWED_UNARYOPS)

# Guard against algorithmic abuse: 9**9**9 would hang the GUI thread.
_MAX_EXPONENT = 64
_MAX_EXPRESSION_LENGTH = 500


@dataclass
class FormulaResult:
    """Outcome of evaluating one derived metric."""

    ok: bool
    value: Optional[float] = None
    expression: str = ""
    #: metric name -> value actually substituted
    resolved: Dict[str, float] = field(default_factory=dict)
    #: constituents we could not find in any data source
    missing: List[str] = field(default_factory=list)
    #: per-constituent provenance, for the audit trail
    sources: Dict[str, str] = field(default_factory=dict)
    error: str = ""

    def describe(self) -> str:
        """Human-readable working, e.g. '120 + 80 + 50 = 250'."""
        if not self.resolved:
            return self.expression
        rendered = self.expression
        # Substitute longest names first so 'revenue' does not corrupt
        # 'total_revenue' during textual replacement.
        for name in sorted(self.resolved, key=len, reverse=True):
            rendered = rendered.replace(name, _trim(self.resolved[name]))
        suffix = f" = {_trim(self.value)}" if self.value is not None else ""
        return rendered + suffix


def _trim(value: Optional[float]) -> str:
    """Compact float rendering for the working-out string."""
    if value is None:
        return "?"
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value)):,}"
    return f"{value:,.4f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# VALIDATION (done once, at load time)
# ---------------------------------------------------------------------------
def validate_expression(expression: str) -> Set[str]:
    """Parse and whitelist-check a formula. Returns its variable names.

    Raises :class:`FormulaError` describing exactly what is wrong, which the
    GUI surfaces to the user so they can fix ``config.json``.
    """
    if not isinstance(expression, str) or not expression.strip():
        raise FormulaError("Formula is empty.")

    if len(expression) > _MAX_EXPRESSION_LENGTH:
        raise FormulaError(
            f"Formula is too long ({len(expression)} chars, "
            f"limit {_MAX_EXPRESSION_LENGTH}).")

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"Syntax error: {exc.msg}") from exc

    names: Set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise FormulaError(
                f"'{type(node).__name__}' is not allowed in a formula. "
                f"Only numbers, metric names, + - * / // % ** and the "
                f"functions {', '.join(sorted(_ALLOWED_FUNCTIONS))} may be used."
            )

        # Constants must be numeric - no strings, no None, no bytes.
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise FormulaError(
                    f"Only numeric literals are allowed, found {node.value!r}.")

        # Calls must target a bare whitelisted function name.
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise FormulaError("Only direct calls to whitelisted functions are allowed.")
            if node.func.id not in _ALLOWED_FUNCTIONS:
                raise FormulaError(
                    f"Unknown function '{node.func.id}'. Allowed: "
                    f"{', '.join(sorted(_ALLOWED_FUNCTIONS))}.")
            if node.keywords:
                raise FormulaError("Keyword arguments are not allowed in formulas.")

        # Collect variable names, ignoring function names.
        if isinstance(node, ast.Name):
            names.add(node.id)

        # Block runaway exponents.
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            right = node.right
            if isinstance(right, ast.Constant) and isinstance(right.value, (int, float)):
                if abs(right.value) > _MAX_EXPONENT:
                    raise FormulaError(
                        f"Exponent {right.value} exceeds the safety limit "
                        f"of {_MAX_EXPONENT}.")
            else:
                raise FormulaError("Exponents must be numeric literals.")

    # Function names are not variables.
    names -= set(_ALLOWED_FUNCTIONS)

    if not names:
        raise FormulaError(
            "Formula references no metrics - it would always produce the same "
            "number, so there is nothing to verify against.")

    return names


# ---------------------------------------------------------------------------
# EVALUATION
# ---------------------------------------------------------------------------
def _eval_node(node: ast.AST, values: Dict[str, float]) -> float:
    """Recursively evaluate a pre-validated AST node."""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, values)

    if isinstance(node, ast.Constant):
        return float(node.value)

    if isinstance(node, ast.Name):
        if node.id not in values:
            raise FormulaError(f"No value available for '{node.id}'.")
        return float(values[node.id])

    if isinstance(node, ast.UnaryOp):
        handler = _ALLOWED_UNARYOPS.get(type(node.op))
        if handler is None:
            raise FormulaError(f"Unsupported unary operator {type(node.op).__name__}.")
        return handler(_eval_node(node.operand, values))

    if isinstance(node, ast.BinOp):
        handler = _ALLOWED_BINOPS.get(type(node.op))
        if handler is None:
            raise FormulaError(f"Unsupported operator {type(node.op).__name__}.")
        left = _eval_node(node.left, values)
        right = _eval_node(node.right, values)
        if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and right == 0:
            raise FormulaError("Division by zero.")
        result = handler(left, right)
        if isinstance(result, complex) or result != result or result in (
                float("inf"), float("-inf")):
            raise FormulaError("Formula produced a non-finite result.")
        return float(result)

    if isinstance(node, ast.Call):
        function = _ALLOWED_FUNCTIONS[node.func.id]  # validated earlier
        arguments = [_eval_node(a, values) for a in node.args]
        try:
            return float(function(*arguments))
        except (TypeError, ValueError) as exc:
            raise FormulaError(f"{node.func.id}() failed: {exc}") from exc

    raise FormulaError(f"Unsupported expression element {type(node).__name__}.")


class FormulaEngine:
    """Validates formulas at startup and evaluates them during verification.

    Constructed once from the ``derived_metrics`` block of ``config.json``.
    Invalid entries are dropped (and reported in :attr:`errors`) rather than
    being allowed to break the application.
    """

    def __init__(self, definitions: Optional[Dict[str, str]] = None) -> None:
        self.definitions: Dict[str, str] = {}
        self.variables: Dict[str, Set[str]] = {}
        self.errors: Dict[str, str] = {}

        for name, expression in (definitions or {}).items():
            key = str(name).strip().lower()
            if not key:
                continue
            try:
                names = validate_expression(str(expression))
            except FormulaError as exc:
                self.errors[key] = str(exc)
                continue

            # A formula that references itself would recurse forever.
            if key in {n.lower() for n in names}:
                self.errors[key] = (
                    f"Formula for '{key}' references itself, which would loop.")
                continue

            self.definitions[key] = str(expression)
            self.variables[key] = names

        # Reject mutually-recursive chains (a -> b -> a).
        self._drop_cycles()

    # -- introspection -----------------------------------------------------
    def has(self, metric: Optional[str]) -> bool:
        return bool(metric) and str(metric).strip().lower() in self.definitions

    def expression_for(self, metric: str) -> str:
        return self.definitions.get(str(metric).strip().lower(), "")

    def constituents(self, metric: str) -> List[str]:
        return sorted(self.variables.get(str(metric).strip().lower(), set()))

    def all_names(self) -> List[str]:
        return sorted(self.definitions)

    def _drop_cycles(self) -> None:
        """Disable EVERY formula caught in a dependency cycle.

        Both members of ``a = b + 1`` / ``b = a + 1`` must go. Dropping only
        one would leave the survivor pointing at a metric that no longer
        exists, which silently degrades to a permanent AMBER instead of a clear
        configuration error the user can act on.

        We compute the full cycle set FIRST against the untouched graph, then
        remove them in one pass.
        """
        def reaches(start: str, target: str, seen: Set[str]) -> bool:
            for var in self.variables.get(start, set()):
                low = var.lower()
                if low == target:
                    return True
                if low in self.definitions and low not in seen:
                    seen.add(low)
                    if reaches(low, target, seen):
                        return True
            return False

        # Snapshot the cyclic keys before mutating anything.
        cyclic = [key for key in self.definitions if reaches(key, key, set())]

        for key in cyclic:
            self.errors[key] = (
                f"Formula for '{key}' forms a circular dependency and was "
                f"disabled.")
            self.definitions.pop(key, None)
            self.variables.pop(key, None)

    # -- evaluation --------------------------------------------------------
    def evaluate(self, metric: str, resolver: Callable[[str], Tuple[Optional[float], str]],
                 _depth: int = 0) -> FormulaResult:
        """Compute a derived metric.

        ``resolver(name)`` is supplied by the verifier and must return
        ``(value_or_None, source_description)`` for a constituent metric. It is
        called once per distinct variable.

        Nested formulas are supported: if a constituent is itself a derived
        metric we recurse (bounded by ``_depth`` to be safe).

        This method NEVER raises - all failures come back as ``ok=False``.
        """
        key = str(metric).strip().lower()
        expression = self.definitions.get(key)
        if expression is None:
            return FormulaResult(False, expression="",
                                 error=f"'{metric}' is not a derived metric.")

        if _depth > 5:
            return FormulaResult(False, expression=expression,
                                 error="Formula nesting is too deep.")

        resolved: Dict[str, float] = {}
        sources: Dict[str, str] = {}
        missing: List[str] = []

        for name in sorted(self.variables.get(key, set())):
            # Nested derived metric?
            if name.lower() in self.definitions:
                nested = self.evaluate(name, resolver, _depth + 1)
                if nested.ok and nested.value is not None:
                    resolved[name] = nested.value
                    sources[name] = f"derived({nested.describe()})"
                else:
                    missing.append(name)
                continue

            try:
                value, source = resolver(name)
            except Exception as exc:  # a resolver bug must not kill the run
                value, source = None, f"resolver error: {exc}"

            if value is None:
                missing.append(name)
            else:
                resolved[name] = float(value)
                sources[name] = source

        if missing:
            return FormulaResult(
                ok=False, expression=expression, resolved=resolved,
                missing=missing, sources=sources,
                error=("Cannot evaluate: no data for "
                       + ", ".join(f"'{m}'" for m in missing)))

        try:
            tree = ast.parse(expression, mode="eval")
            value = _eval_node(tree, resolved)
        except FormulaError as exc:
            return FormulaResult(False, expression=expression, resolved=resolved,
                                 sources=sources, error=str(exc))
        except Exception as exc:  # belt and braces
            return FormulaResult(False, expression=expression, resolved=resolved,
                                 sources=sources,
                                 error=f"Unexpected formula failure: {exc}")

        return FormulaResult(True, value=value, expression=expression,
                             resolved=resolved, sources=sources)
