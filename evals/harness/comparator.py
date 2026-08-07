"""Execution accuracy: does the agent's result set match the gold result set?

Deterministic, offline, and unit-tested -- this module is the definition of
"correct" for tiers 1-3, so it runs in CI on synthetic fixtures and never needs a
database of its own.

## Decisions, and why

**Column names are ignored.** `COUNT(*) AS shots` and `COUNT(*) AS n` are the same
answer. Only position and value matter.

**Column *count* is not ignored.** A result with an extra column is a different
answer shape, not a superset of the right one -- an agent that returns
(player, points, rank) where the gold returns (player, points) has answered a
different question, and letting that pass would erase the distinction the tier-3
cases exist to test. This matches standard execution-accuracy practice.

**Multiset (bag), not set.** Duplicate rows are meaningful in SQL: a query that
returns a row three times differs from one that returns it once. Set semantics
would let a spurious DISTINCT -- or a missing GROUP BY that happens to collapse
duplicates -- score as correct. Bag semantics is the stricter and more honest
choice, and it is the one place where a reasonable person might pick differently,
so it is stated here rather than left implicit.

**Row order is ignored unless the gold query has ORDER BY** (`order_matters: true`
on the case). Postgres makes no ordering promise without ORDER BY, so comparing
order on an unordered query would score flakiness.

**Numbers compare with tolerance 1e-6; types are coerced across the numeric tower.**
Postgres hands back Decimal for NUMERIC, int for INTEGER, float for DOUBLE
PRECISION, and the same correct answer can arrive as any of them depending on how
the query was written. `bool` is excluded from the numeric tower on purpose -- in
Python it is a subclass of int, and letting True == 1 compare equal would mean a
boolean flag column silently matching a count.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

FLOAT_TOLERANCE = 1e-6

# Rounding quantum for the sort key used in order-insensitive comparison. Coarser
# than the tolerance so that two values which compare equal cannot land in
# different sort buckets over the range of magnitudes this dataset produces.
_SORT_QUANTUM = 1e-9

Row = Sequence[object]


@dataclass(frozen=True)
class Mismatch:
    kind: str
    detail: str


@dataclass(frozen=True)
class Comparison:
    matches: bool
    mismatch: Mismatch | None = None

    @classmethod
    def ok(cls) -> "Comparison":
        return cls(matches=True)

    @classmethod
    def fail(cls, kind: str, detail: str) -> "Comparison":
        return cls(matches=False, mismatch=Mismatch(kind=kind, detail=detail))


def _is_number(value: object) -> bool:
    # bool before int: isinstance(True, int) is True and we do not want that here.
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float, Decimal))


def values_equal(left: object, right: object) -> bool:
    if left is None or right is None:
        return left is None and right is None

    if _is_number(left) and _is_number(right):
        a, b = float(left), float(right)
        if a != a or b != b:  # NaN: never equal, not even to itself
            return False
        return abs(a - b) <= FLOAT_TOLERANCE

    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right

    # Dates, times, strings, and anything else Postgres hands back: exact equality,
    # falling back to string form so a date and its ISO text compare equal.
    if type(left) is type(right):
        return left == right
    return str(left) == str(right)


def rows_equal(left: Row, right: Row) -> bool:
    if len(left) != len(right):
        return False
    return all(values_equal(a, b) for a, b in zip(left, right))


def _sort_key(row: Row) -> tuple:
    """A total order over heterogeneous rows, stable under the float tolerance.

    Numbers are bucketed to a quantum finer than the tolerance and tagged so they
    sort together regardless of whether they arrived as int, float, or Decimal;
    everything else sorts by (type tag, string form). NULLs sort first.
    """
    key: list[tuple[int, object]] = []
    for value in row:
        if value is None:
            key.append((0, ""))
        elif _is_number(value):
            quantized = round(float(value) / _SORT_QUANTUM)
            key.append((1, quantized))
        elif isinstance(value, bool):
            key.append((2, str(value)))
        else:
            key.append((3, str(value)))
    return tuple(key)


def compare_results(
    gold_rows: Sequence[Row],
    agent_rows: Sequence[Row],
    *,
    order_matters: bool = False,
) -> Comparison:
    """Compare two result sets under the semantics documented at module top."""
    if len(gold_rows) != len(agent_rows):
        return Comparison.fail(
            "row_count",
            f"gold returned {len(gold_rows)} row(s), agent returned {len(agent_rows)}",
        )

    if not gold_rows:
        return Comparison.ok()

    gold_width = len(gold_rows[0])
    agent_width = len(agent_rows[0])
    if gold_width != agent_width:
        return Comparison.fail(
            "column_count",
            f"gold returned {gold_width} column(s), agent returned {agent_width}",
        )

    if order_matters:
        left, right = list(gold_rows), list(agent_rows)
    else:
        left = sorted(gold_rows, key=_sort_key)
        right = sorted(agent_rows, key=_sort_key)

    for index, (gold_row, agent_row) in enumerate(zip(left, right)):
        if not rows_equal(gold_row, agent_row):
            where = f"at row {index}" if order_matters else f"at sorted position {index}"
            return Comparison.fail(
                "row_values",
                f"{where}: gold {tuple(gold_row)!r} != agent {tuple(agent_row)!r}",
            )

    return Comparison.ok()
