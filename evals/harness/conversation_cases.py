"""Loader for evals/conversation-*.yaml — multi-turn case sets.

Separate from cases.py, and deliberately so: `text2sql-v0.yaml` stays untouched in
Phase 5 (.agents/p5_conversational_context.md "Separate suite and case format") so
single-turn regressions remain directly comparable with the Phase 4 baseline. Two
loaders that share validation *rules* but not a file format is the cheaper of the
two mistakes available here.

`ConversationTurnCase` is structurally compatible with `cases.EvalCase` — same
`expects`/`gold_sql`/`rubric`/`order_matters`/`accepted_outcomes` surface — which is
what lets `scoring.score()` compute per-turn metrics with no changes at all.

Validation is strict and happens at load time, for the same reason as the
single-turn loader: a malformed case should fail the free offline test, not surface
as a mysterious miss partway into a paid run.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .cases import VALID_EXPECTS, VALID_TIERS, Expects

TURN_KEYS = frozenset(
    {"user", "expects", "gold_sql", "rubric", "notes", "order_matters", "also_accepts", "reset"}
)
CASE_KEYS = frozenset({"id", "tier", "notes", "turns"})


class ConversationCaseError(ValueError):
    """A multi-turn case file that cannot be trusted to score anything."""


@dataclass(frozen=True)
class ConversationTurnCase:
    """One user turn plus what the assistant is expected to do with it.

    `follows_clarify` and `reset` are what make the two conversation-only metrics
    computable. `follows_clarify` is DERIVED, not declared: it is true exactly when
    the previous turn expected a clarification, and deriving it means a case file
    cannot claim a continuation that its own turn sequence does not contain.
    """

    id: str
    conversation_id: str
    index: int
    tier: int
    question: str
    expects: Expects
    gold_sql: str | None = None
    rubric: str | None = None
    notes: str | None = None
    order_matters: bool = False
    also_accepts: tuple[Expects, ...] = ()
    reset: bool = False
    follows_clarify: bool = False
    # Present only so a turn can stand in for an EvalCase wherever the single-turn
    # scoring code reads one. Conversation cases carry no valid-but-wrong traps in
    # this phase (the plan defers them).
    trap: bool = False

    @property
    def is_execution_scored(self) -> bool:
        return self.expects == "answer"

    @property
    def accepted_outcomes(self) -> frozenset[str]:
        return frozenset({self.expects, *self.also_accepts})


@dataclass(frozen=True)
class ConversationCase:
    id: str
    tier: int
    turns: tuple[ConversationTurnCase, ...]
    notes: str | None = None


def _require(cond: bool, case_id: str, message: str) -> None:
    if not cond:
        raise ConversationCaseError(f"case {case_id!r}: {message}")


def _parse_turn(
    raw: dict[str, Any], *, case_id: str, tier: int, index: int, follows_clarify: bool
) -> ConversationTurnCase:
    turn_id = f"{case_id}#{index + 1}"
    _require(isinstance(raw, dict), case_id, f"turn {index + 1} must be a mapping")

    unknown = set(raw) - TURN_KEYS
    _require(not unknown, turn_id, f"unknown keys {sorted(unknown)}")

    question = raw.get("user")
    _require(
        isinstance(question, str) and question.strip() != "",
        turn_id,
        "`user` must be a non-empty string",
    )

    expects = raw.get("expects")
    _require(
        expects in VALID_EXPECTS,
        turn_id,
        f"expects must be one of {sorted(VALID_EXPECTS)}, got {expects!r}",
    )

    gold_sql = raw.get("gold_sql")
    _require(gold_sql is None or isinstance(gold_sql, str), turn_id, "gold_sql must be a string or null")
    gold_sql = gold_sql.strip() if isinstance(gold_sql, str) and gold_sql.strip() else None

    rubric = raw.get("rubric")
    order_matters = raw.get("order_matters", False)
    _require(isinstance(order_matters, bool), turn_id, "order_matters must be a boolean")

    reset = raw.get("reset", False)
    _require(isinstance(reset, bool), turn_id, "reset must be a boolean")
    # A first turn has no prior topic to leak, so scoring it as a reset would
    # inflate context-reset accuracy with turns that never tested anything.
    _require(not reset or index > 0, turn_id, "reset is meaningless on the first turn")

    if expects == "answer":
        _require(gold_sql is not None, turn_id, "expects: answer requires gold_sql")
    else:
        _require(
            gold_sql is None,
            turn_id,
            f"expects: {expects} must not carry gold_sql (nothing would execute it)",
        )
        _require(
            isinstance(rubric, str) and rubric.strip() != "",
            turn_id,
            f"expects: {expects} requires a rubric",
        )

    _require(
        not order_matters or expects == "answer",
        turn_id,
        "order_matters is only meaningful with expects: answer",
    )

    also_accepts = raw.get("also_accepts", []) or []
    _require(isinstance(also_accepts, list), turn_id, "also_accepts must be a list")
    for alt in also_accepts:
        _require(
            alt in VALID_EXPECTS,
            turn_id,
            f"also_accepts entries must be one of {sorted(VALID_EXPECTS)}, got {alt!r}",
        )
        _require(alt != expects, turn_id, f"also_accepts repeats expects ({alt!r})")
    # Same rule as the single-turn loader: an answerable turn that accepts an
    # abstention makes false abstention unmeasurable. A continuation turn that
    # narrows instead of answering is credited by the clarification-continuation
    # metric, which is scored separately for exactly this reason.
    _require(
        not also_accepts or expects != "answer",
        turn_id,
        "also_accepts is not allowed on expects: answer",
    )

    return ConversationTurnCase(
        id=turn_id,
        conversation_id=case_id,
        index=index,
        tier=tier,
        question=" ".join(question.split()),
        expects=expects,  # type: ignore[arg-type]
        gold_sql=gold_sql,
        rubric=rubric.strip() if isinstance(rubric, str) else None,
        notes=raw.get("notes"),
        order_matters=order_matters,
        also_accepts=tuple(also_accepts),
        reset=reset,
        follows_clarify=follows_clarify,
    )


def parse_conversation_case(raw: dict[str, Any]) -> ConversationCase:
    case_id = raw.get("id")
    if not isinstance(case_id, str) or not case_id:
        raise ConversationCaseError(f"case is missing a string `id`: {raw!r}")

    unknown = set(raw) - CASE_KEYS
    _require(not unknown, case_id, f"unknown keys {sorted(unknown)}")

    tier = raw.get("tier")
    _require(
        tier in VALID_TIERS, case_id, f"tier must be one of {sorted(VALID_TIERS)}, got {tier!r}"
    )

    raw_turns = raw.get("turns")
    _require(isinstance(raw_turns, list), case_id, "`turns` must be a list")
    # A one-turn conversation is a single-turn case wearing a costume, and it
    # belongs in text2sql-v0.yaml where it stays comparable with the baseline.
    _require(len(raw_turns) >= 2, case_id, "a conversation needs at least two turns")

    turns: list[ConversationTurnCase] = []
    previous_expects: str | None = None
    for index, raw_turn in enumerate(raw_turns):
        turn = _parse_turn(
            raw_turn,
            case_id=case_id,
            tier=int(tier),  # type: ignore[arg-type]
            index=index,
            follows_clarify=previous_expects == "clarify",
        )
        turns.append(turn)
        previous_expects = turn.expects

    return ConversationCase(
        id=case_id, tier=int(tier), turns=tuple(turns), notes=raw.get("notes")  # type: ignore[arg-type]
    )


def load_conversation_cases(path: str | Path) -> list[ConversationCase]:
    path = Path(path)
    with path.open() as handle:
        doc = yaml.safe_load(handle)

    if not isinstance(doc, dict) or "cases" not in doc:
        raise ConversationCaseError(
            f"{path}: top-level document must be a mapping with a `cases` key"
        )

    raw_cases = doc["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ConversationCaseError(f"{path}: `cases` must be a non-empty list")

    cases = [parse_conversation_case(raw) for raw in raw_cases]

    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise ConversationCaseError(f"{path}: duplicate case id {case.id!r}")
        seen.add(case.id)

    return cases
