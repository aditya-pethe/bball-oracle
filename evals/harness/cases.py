"""Loader for evals/*.yaml case sets.

Validation is strict and happens at load time, not at run time: a malformed case
should fail the (free, offline) loader test rather than surface as a mysterious miss
forty dollars into a model sweep.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

Expects = Literal["answer", "clarify", "decline"]

VALID_EXPECTS: frozenset[str] = frozenset({"answer", "clarify", "decline"})
VALID_TIERS: frozenset[int] = frozenset({1, 2, 3, 4, 5})


class CaseError(ValueError):
    """A case file that cannot be trusted to score anything."""


@dataclass(frozen=True)
class EvalCase:
    id: str
    tier: int
    question: str
    expects: Expects
    gold_sql: str | None = None
    rubric: str | None = None
    notes: str | None = None
    order_matters: bool = False
    trap: bool = False
    # Additional outcomes that also pass. Exists because some questions have more
    # than one defensible response -- "which player improved the most?" with a
    # single season loaded is arguably ambiguous (clarify) AND arguably
    # unanswerable (decline), and a set that scored only one of those would be
    # penalising a correct answer. Deliberately NOT allowed on `answer` cases:
    # widening those would let an agent duck an answerable question and pass.
    also_accepts: tuple[Expects, ...] = ()

    @property
    def is_execution_scored(self) -> bool:
        """True when execution accuracy applies; False for judgment-scored cases."""
        return self.expects == "answer"

    @property
    def accepted_outcomes(self) -> frozenset[str]:
        return frozenset({self.expects, *self.also_accepts})


def _require(cond: bool, case_id: str, message: str) -> None:
    if not cond:
        raise CaseError(f"case {case_id!r}: {message}")


def parse_case(raw: dict[str, Any]) -> EvalCase:
    case_id = raw.get("id")
    if not isinstance(case_id, str) or not case_id:
        raise CaseError(f"case is missing a string `id`: {raw!r}")

    unknown = set(raw) - {
        "id", "tier", "question", "expects", "gold_sql",
        "rubric", "notes", "order_matters", "trap", "also_accepts",
    }
    _require(not unknown, case_id, f"unknown keys {sorted(unknown)}")

    tier = raw.get("tier")
    _require(tier in VALID_TIERS, case_id, f"tier must be one of {sorted(VALID_TIERS)}, got {tier!r}")

    question = raw.get("question")
    _require(
        isinstance(question, str) and question.strip() != "",
        case_id,
        "question must be a non-empty string",
    )

    expects = raw.get("expects")
    _require(
        expects in VALID_EXPECTS,
        case_id,
        f"expects must be one of {sorted(VALID_EXPECTS)}, got {expects!r}",
    )

    gold_sql = raw.get("gold_sql")
    _require(
        gold_sql is None or isinstance(gold_sql, str),
        case_id,
        "gold_sql must be a string or null",
    )
    gold_sql = gold_sql.strip() if isinstance(gold_sql, str) and gold_sql.strip() else None

    rubric = raw.get("rubric")
    order_matters = raw.get("order_matters", False)
    _require(isinstance(order_matters, bool), case_id, "order_matters must be a boolean")

    trap = raw.get("trap", False)
    _require(isinstance(trap, bool), case_id, "trap must be a boolean")

    # The two invariants that make scoring possible at all. An `answer` case with no
    # gold query cannot be scored by execution accuracy, and a clarify/decline case
    # with no rubric cannot be judged -- both would silently score as something.
    if expects == "answer":
        _require(gold_sql is not None, case_id, "expects: answer requires gold_sql")
    else:
        _require(
            gold_sql is None,
            case_id,
            f"expects: {expects} must not carry gold_sql (nothing would execute it)",
        )
        _require(
            isinstance(rubric, str) and rubric.strip() != "",
            case_id,
            f"expects: {expects} requires a rubric",
        )

    # order_matters only has meaning where rows are compared.
    _require(
        not order_matters or expects == "answer",
        case_id,
        "order_matters is only meaningful with expects: answer",
    )

    also_accepts = raw.get("also_accepts", []) or []
    _require(isinstance(also_accepts, list), case_id, "also_accepts must be a list")
    for alt in also_accepts:
        _require(
            alt in VALID_EXPECTS, case_id,
            f"also_accepts entries must be one of {sorted(VALID_EXPECTS)}, got {alt!r}",
        )
        _require(alt != expects, case_id, f"also_accepts repeats expects ({alt!r})")
    # An answerable question must be answered. Widening it to accept an
    # abstention would make the false-abstention metric unmeasurable.
    _require(
        not also_accepts or expects != "answer",
        case_id,
        "also_accepts is not allowed on expects: answer -- an answerable "
        "question that accepts an abstention cannot score false abstention",
    )

    return EvalCase(
        id=case_id,
        tier=int(tier),  # type: ignore[arg-type]
        question=" ".join(question.split()),  # YAML folded scalars keep line breaks
        expects=expects,  # type: ignore[arg-type]
        gold_sql=gold_sql,
        rubric=rubric.strip() if isinstance(rubric, str) else None,
        notes=raw.get("notes"),
        order_matters=order_matters,
        trap=trap,
        also_accepts=tuple(also_accepts),
    )


def load_cases(path: str | Path) -> list[EvalCase]:
    path = Path(path)
    with path.open() as handle:
        doc = yaml.safe_load(handle)

    if not isinstance(doc, dict) or "cases" not in doc:
        raise CaseError(f"{path}: top-level document must be a mapping with a `cases` key")

    raw_cases = doc["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise CaseError(f"{path}: `cases` must be a non-empty list")

    cases = [parse_case(raw) for raw in raw_cases]

    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise CaseError(f"{path}: duplicate case id {case.id!r}")
        seen.add(case.id)

    return cases
