"""Graph state shared by every node.

TypedDict rather than a dataclass because LangGraph merges each node's return
value into it key-by-key; `Annotated[..., operator.add]` marks the fields that
must accumulate across nodes (per-node timings, token usage, retry counters).
Every other field is last-write-wins, which is what a retry needs -- draft_sql
producing a corrected `sql` should simply replace the rejected one, not append
to it.

`node_timings` is what the response envelope's per-node latency metric
(.agents/p4_agent.md's metric table: "instrument per graph node, not just
total") is built from. Nodes don't populate it themselves -- see agent/graph.py's
`_timed` wrapper, which measures wall time around every node and is the only
thing that writes this key.
"""
from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

from .envelope import ExecResult, NodeTiming

Outcome = Literal["answer", "clarify", "decline"]


class AgentState(TypedDict, total=False):
    # Set once, before the graph runs.
    question: str
    user_id: int
    conversation_id: str | None

    # classify's verdict. Left unset (falls through to draft_sql) when the
    # question is answerable; set to "clarify"/"decline" to short-circuit
    # straight to the matching terminal node, before any SQL is written.
    outcome: Outcome | None
    clarify_question: str | None
    decline_reason: str | None

    # draft_sql -> execute -> critic pipeline. All last-write-wins:
    # a retry's draft_sql call is expected to overwrite `sql` and clear
    # whichever error/verdict field sent it back here.
    sql: str | None
    validation_error: str | None
    result: ExecResult | None
    execute_error: str | None
    critic_verdict: Literal["ok", "reject"] | None
    # Set when a failure cannot be repaired by redrafting (a malformed
    # request, an unknown user). Short-circuits the correction ladder.
    fatal_error: str | None
    critic_feedback: str | None

    # Final answer, set by summarize (or by clarify/decline directly).
    summary: str | None
    error: str | None

    # Bounded retry (Correction ladder, .agents/p4_agent.md). Counted per rung
    # so the harness can attribute which rung fired, on top of the combined
    # cap (agent/graph.py's RetryLimits.total_model_calls) that bounds the
    # rungs stacking together. Rungs 1-3 all report through execute_retry_count:
    # validator rejection is a server response, not a separate local stage.
    execute_retry_count: Annotated[int, operator.add]
    critic_retry_count: Annotated[int, operator.add]
    total_model_calls: Annotated[int, operator.add]

    # Instrumentation surfaced in the response envelope.
    node_timings: Annotated[list[NodeTiming], operator.add]
    input_tokens: Annotated[int, operator.add]
    output_tokens: Annotated[int, operator.add]
    cache_read_input_tokens: Annotated[int, operator.add]


def new_state(question: str, user_id: int, conversation_id: str | None = None) -> AgentState:
    """The initial state every graph run must start from.

    The `Annotated[..., operator.add]` fields need a seed value -- the first
    node to write one is adding to it, not setting it from nothing.
    """
    return AgentState(
        question=question,
        user_id=user_id,
        conversation_id=conversation_id,
        outcome=None,
        clarify_question=None,
        decline_reason=None,
        sql=None,
        validation_error=None,
        result=None,
        execute_error=None,
        critic_verdict=None,
        fatal_error=None,
        critic_feedback=None,
        summary=None,
        error=None,
        execute_retry_count=0,
        critic_retry_count=0,
        total_model_calls=0,
        node_timings=[],
        input_tokens=0,
        output_tokens=0,
        cache_read_input_tokens=0,
    )
