"""classify -- answerable / ambiguous / unanswerable, before any SQL is written.

Catches tiers 4-5 where ambiguity is cheapest to handle (.agents/p4_agent.md
"Agent loop"). The heavy lifting -- what counts as genuinely ambiguous vs. a
defensible judgment call, what "the data can't answer this" means for this
schema -- lives in agent/schema_prompt.py's OUTCOME_GUIDANCE, which is already
part of the cached system prefix every node gets via ModelClient.complete()
(agent/llm.py). This node's user turn only states the task; it does not
restate that guidance, both because duplicating it would waste tokens on every
call and because a second copy could drift from the one in the cached prefix.

The zero-shot baseline (agent/baseline.py) proves this framing already scores
100% abstention precision / 0% false-abstention in a single call that does
both classification AND drafting. Splitting classification into its own node
must not lose that -- it is strictly a narrower task than the baseline's (pure
verdict, no SQL to write in the same breath), so it should be at least as
good, not a new place to regress.

Schema deviates from the original stub: `clarify_question` / `decline_reason`
are dedicated fields instead of one shared `reason`, matching the plan's
language ("the specific question you need answered" / "plainly what is
missing") -- a single free-text field forced the node to reuse one string for
two different jobs.
"""
from __future__ import annotations

from typing import Callable

from ..context import FOLLOW_UP_GUIDANCE, context_of, node_messages, task_question
from ..llm import ModelClient
from ..state import AgentState

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["answerable", "ambiguous", "unanswerable"],
            "description": "answerable: the question is well-defined and the "
                           "data can answer it -- SQL will be drafted next. "
                           "ambiguous: genuinely ambiguous along an axis that "
                           "changes the answer, not merely a judgment call you "
                           "could defensibly make yourself. unanswerable: the "
                           "two tables cannot answer this at all (wrong season, "
                           "playoffs, biography, awards, salary, etc).",
        },
        "clarify_question": {
            "type": ["string", "null"],
            "description": "Required when verdict is 'ambiguous': the specific "
                           "question to ask the user, or the definition you "
                           "would adopt and why. Null otherwise.",
        },
        "decline_reason": {
            "type": ["string", "null"],
            "description": "Required when verdict is 'unanswerable': plainly "
                           "what is missing from the data. May offer a "
                           "statistical proxy if one exists, clearly labeled as "
                           "a proxy. Null otherwise.",
        },
    },
    "required": ["verdict", "clarify_question", "decline_reason"],
    "additionalProperties": False,
}


def build(model_client: ModelClient) -> Callable[[AgentState], dict]:
    def classify(state: AgentState) -> dict:
        parts = [
            "Task: decide only whether the question below is answerable from "
            "the schema above, before any SQL is written. Do not draft SQL."
        ]
        # Only when there IS a conversation -- `has_exchange`, not `turns`. A window
        # of unanswered questions is not history, and instructing the model to
        # resolve references against it is worse than sending nothing
        # (.agents/p5_regression_report.md, 2026-08-10).
        context = context_of(state)
        if context is not None and context.has_exchange:
            parts.append(FOLLOW_UP_GUIDANCE)
        parts.append(f"Question: {task_question(state)}")
        content = "\n\n".join(parts)

        reply = model_client.complete(
            "classify",
            messages=node_messages(state, "classify", content),
            schema=RESPONSE_SCHEMA,
        )
        verdict = reply.payload.get("verdict", "answerable")

        update: dict = {
            "total_model_calls": 1,
            "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens,
            "cache_creation_input_tokens": reply.cache_creation_input_tokens,
            "cache_read_input_tokens": reply.cache_read_input_tokens,
        }
        if verdict == "ambiguous":
            update["outcome"] = "clarify"
            update["clarify_question"] = (
                reply.payload.get("clarify_question") or "Could you clarify what you mean?"
            )
        elif verdict == "unanswerable":
            update["outcome"] = "decline"
            update["decline_reason"] = (
                reply.payload.get("decline_reason")
                or "The available data cannot answer this question."
            )
        # verdict == "answerable": outcome stays unset, falls through to draft_sql.
        return update

    return classify

