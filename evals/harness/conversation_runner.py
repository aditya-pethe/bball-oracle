"""Runs multi-turn cases against a conversational agent.

The single-turn `Agent` protocol (evals/harness/envelope.py) is unchanged and still
what `runner.py` drives. This module adds the one thing it cannot express: a
conversation, where turn 2's answer depends on turn 1 having happened.

The split is `ConversationAgent` (opens threads) and `ConversationSession` (one
thread). That is the shape the thing being measured actually has — production opens
a conversation and asks into it — and it makes the isolation requirement structural:
a new session cannot see another session's turns because it never holds them.

Like runner.py, this needs the live dataset and costs real money per run. Never CI.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from .comparator import compare_results
from .conversation_cases import ConversationCase, ConversationTurnCase, load_conversation_cases
from .envelope import Agent, AgentEnvelope
from .executors import DirectExecutor, SqlExecutor
from .scoring import CaseResult


@runtime_checkable
class ConversationSession(Protocol):
    """One thread. Successive `ask` calls share context; sessions never do."""

    def ask(self, question: str) -> AgentEnvelope: ...


@runtime_checkable
class ConversationAgent(Protocol):
    name: str

    def new_conversation(self) -> ConversationSession: ...


@dataclass
class ConversationResult:
    case: ConversationCase
    turns: list[CaseResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Whole-conversation success: every scored turn passed.

        Deliberately unforgiving. A thread where turn 1 was right and the follow-up
        was wrong is not "50% useful" to somebody having the conversation — they got
        a wrong answer — and turn-level accuracy already reports the partial credit.
        """
        return all(turn.passed for turn in self.turns)


class StatelessConversationAgent:
    """Runs a single-turn `Agent` through the multi-turn suite with no context at all.

    The control: it answers each turn as if it were the first, so the difference
    between it and the real conversational agent IS what carrying context bought.
    Without a control, "72% follow-up accuracy" has nothing to be compared against.
    """

    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    @property
    def name(self) -> str:
        return f"{self._agent.name}-no-context"

    def new_conversation(self) -> ConversationSession:
        agent = self._agent

        class _Session:
            def ask(self, question: str) -> AgentEnvelope:
                return agent.answer(question)

        return _Session()


def score_turn(
    turn: ConversationTurnCase, envelope: AgentEnvelope, gold_executor: SqlExecutor
) -> CaseResult:
    """Identical scoring to a single-turn case — same gold execution, same
    comparator, same abstention handling. A follow-up is not graded on a curve."""
    if not turn.is_execution_scored:
        return CaseResult(case=turn, envelope=envelope)  # type: ignore[arg-type]

    assert turn.gold_sql is not None  # guaranteed by the loader

    gold = gold_executor.execute(turn.gold_sql)
    if not gold.ok:
        return CaseResult(  # type: ignore[arg-type]
            case=turn, envelope=envelope, gold_error=f"{gold.status}: {gold.error}"
        )

    if envelope.outcome != "answer" or envelope.result is None:
        return CaseResult(case=turn, envelope=envelope)  # type: ignore[arg-type]

    comparison = compare_results(
        gold.result.rows if gold.result else (),
        envelope.result.rows,
        order_matters=turn.order_matters,
    )
    return CaseResult(case=turn, envelope=envelope, comparison=comparison)  # type: ignore[arg-type]


def run_conversation(
    case: ConversationCase,
    agent: ConversationAgent,
    gold_executor: SqlExecutor,
    *,
    pace_seconds: float = 0.0,
) -> ConversationResult:
    session = agent.new_conversation()
    result = ConversationResult(case=case)

    for index, turn in enumerate(case.turns):
        envelope = session.ask(turn.question)
        result.turns.append(score_turn(turn, envelope, gold_executor))
        # A missed turn does NOT abandon the conversation. The follow-up's own
        # accuracy is the measurement this suite exists for, and stopping early
        # would silently drop exactly the turns that matter most.
        if pace_seconds and index < len(case.turns) - 1:
            time.sleep(pace_seconds)

    return result


def run_conversation_suite(
    agent: ConversationAgent,
    case_file: str | Path,
    *,
    gold_executor: SqlExecutor | None = None,
    pace_seconds: float = 2.5,
    only: list[str] | None = None,
    on_conversation: object = None,
) -> list[ConversationResult]:
    cases = load_conversation_cases(case_file)
    if only:
        wanted = set(only)
        cases = [c for c in cases if c.id in wanted]
        missing = wanted - {c.id for c in cases}
        if missing:
            raise ValueError(f"no such conversation id(s): {sorted(missing)}")

    executor = gold_executor or DirectExecutor()
    results: list[ConversationResult] = []

    for index, case in enumerate(cases):
        result = run_conversation(case, agent, executor, pace_seconds=pace_seconds)
        results.append(result)

        if callable(on_conversation):
            on_conversation(case, result)

        if pace_seconds and index < len(cases) - 1:
            time.sleep(pace_seconds)

    return results
