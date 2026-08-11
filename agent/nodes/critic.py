"""critic -- re-reads (question, SQL, result shape + a small sample) and rules
on whether they match. The node aimed squarely at valid-but-wrong, the most
dangerous failure mode in the plan's ranking (.agents/p4_agent.md design
principle 2).

A trigger-happy critic is worse than none: it burns a bounded retry (rung 4,
capped at one) and can turn a right answer into a wrong one by sending a
correct draft back for no reason. The schema below is written to bias toward
`ok` -- reject requires naming a *concrete* mismatch, and the description
spells out that a defensible reading of an ambiguous question, a stylistic
difference, or a reasonable threshold choice is not grounds for rejection.

Deviates from the stub's docstring in one respect: it includes a small sample
of the actual rows (first 3), not shape alone. The stub's reasoning --
"shouldn't burn tokens re-reading what execute already fetched" -- still
holds for the *full* result (never sent), but a 3-row sample is a handful of
tokens and is what lets the critic catch things shape alone cannot: an
all-NULL column, an obviously wrong magnitude, a column that clearly isn't
what was asked for. The classic trap case (t3-scoring-leaders-TRAP, missing
free throws) is actually caught from the SQL text against the system prompt's
GOTCHAS section, not from the sample -- but the sample earns its keep on
other failure shapes shape-only review would miss.
"""
from __future__ import annotations

from typing import Callable

from ..context import node_messages, task_question
from ..llm import ModelClient
from ..state import AgentState
from ..prompts import render

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["ok", "reject"],
            "description": "reject ONLY if you can name a concrete, specific "
                           "mismatch between the question and what this SQL "
                           "computes or what it returned (wrong table for the "
                           "quantity asked, wrong aggregation, wrong entity, "
                           "wrong row/column count for what was asked). "
                           "Default to ok -- a defensible reading of an "
                           "ambiguous question, a stylistic difference, or a "
                           "reasonable threshold choice is NOT grounds for "
                           "rejection. When in doubt, ok.",
        },
        "feedback": {
            "type": "string",
            "description": "If verdict is reject: the specific mismatch, "
                           "stated precisely enough that a redraft can fix it "
                           "(e.g. 'computes points from shot_detail only, "
                           "missing free throws from pbp_event'). Empty string "
                           "if verdict is ok.",
        },
    },
    "required": ["verdict", "feedback"],
    "additionalProperties": False,
}

_SAMPLE_ROWS = 3


def _result_summary(state: AgentState) -> str:
    result = state.get("result")
    if result is None:
        return "no result"
    sample = result.rows[:_SAMPLE_ROWS]
    lines = [f"columns={result.columns!r}, {result.row_count} row(s) total"]
    if sample:
        lines.append(f"first {len(sample)} row(s): {sample!r}")
    return "\n".join(lines)


def build(model_client: ModelClient) -> Callable[[AgentState], dict]:
    def critic(state: AgentState) -> dict:
        content = render(
            "critic",
            question=task_question(state),
            sql=state.get("sql"),
            result=_result_summary(state),
        )
        reply = model_client.complete(
            "critic",
            messages=node_messages(state, "critic", content),
            schema=RESPONSE_SCHEMA,
        )
        verdict = reply.payload.get("verdict", "ok")

        update: dict = {
            "critic_verdict": verdict,
            "critic_feedback": reply.payload.get("feedback") or None,
            "total_model_calls": 1,
            "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens,
            "cache_creation_input_tokens": reply.cache_creation_input_tokens,
            "cache_read_input_tokens": reply.cache_read_input_tokens,
        }
        if verdict == "reject":
            update["critic_retry_count"] = 1
        return update

    return critic
