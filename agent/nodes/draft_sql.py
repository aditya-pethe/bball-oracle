"""draft_sql -- writes the SQL. Schema is in the cached system prefix
(agent/schema_prompt.py, reused via ModelClient.complete -- 2 tables, no RAG,
no embeddings needed -- .agents/p4_agent.md).

This is the node every retry rung (1-4 in the Correction ladder) routes back
to, so it doubles as the correction step. The plan's hard requirement is that
a retry "MUST receive the specific failure that sent it back ... and address
it. A retry that redrafts blind is worthless." Each call to ModelClient.complete
is a fresh, single-turn request -- there is no assistant turn from the prior
attempt in the messages list for the model to remember on its own -- so
`_retry_instruction` below puts both the specific error/verdict AND the exact
prior SQL text into the user turn. Without the prior SQL, "fix a validator
rejection" is not addressable: the model has no idea what it wrote.

`outcome`/`summary` are deliberately NOT set here (unlike agent/baseline.py's
single-call ZeroShotBaseline, which has no separate summarize node to defer
to). Narration belongs to summarize, which reads the actual result: baking a
summary into draft_sql would mean describing an answer before it's known the
query even runs. Any threshold/definition the drafter chooses (per the shared
RULES section) is asked to travel as a leading SQL comment instead, since
summarize reads the SQL text and that keeps the choice visible to both the
user (SQL is always shown, principle 1) and the summarizer, without adding a
field to the schema.
"""
from __future__ import annotations

from typing import Callable

from ..context import TRANSFORM_GUIDANCE, context_of, node_messages, task_question
from ..llm import ModelClient
from ..state import AgentState

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "sql": {
            "type": "string",
            "description": "A single PostgreSQL SELECT statement answering the "
                           "question, per the schema and rules in the system "
                           "prompt. If you adopt a threshold, definition, or "
                           "default the question didn't specify, document it "
                           "in a leading SQL comment (-- ...) so it stays "
                           "visible to the user and survives into the summary.",
        }
    },
    "required": ["sql"],
    "additionalProperties": False,
}


def _retry_instruction(state: AgentState) -> str | None:
    prior_sql = state.get("sql")
    validation_error = state.get("validation_error")
    execute_error = state.get("execute_error")
    critic_feedback = state.get("critic_feedback")

    # Rung 2: validator rejection (server-side libpg-query, via /api/internal/query).
    if validation_error is not None:
        return (
            f"Your previous SQL was rejected by the validator before it ran:\n"
            f"{validation_error}\n\n"
            f"Previous SQL:\n{prior_sql}\n\n"
            "Fix it -- almost certainly it wasn't a single, schema-qualified "
            "SELECT statement. Address this specific rejection, don't just "
            "rewrite from scratch."
        )
    # Rungs 1 and 3 both surface as execute_error, distinguished by message.
    if execute_error is not None:
        if execute_error == "query returned zero rows":
            return (
                "Your previous SQL ran without error but returned zero rows:\n\n"
                f"Previous SQL:\n{prior_sql}\n\n"
                "That is almost always a filter, join key, or literal-matching "
                "problem (e.g. a name spelled differently than it's stored, an "
                "overly strict WHERE, a bad join condition) -- find and fix the "
                "specific cause rather than redrafting from scratch."
            )
        return (
            f"Your previous SQL failed when it ran:\n{execute_error}\n\n"
            f"Previous SQL:\n{prior_sql}\n\n"
            "Fix the specific error above -- most likely a column name, type "
            "mismatch, or syntax mistake."
        )
    # Rung 4: critic rejection.
    if critic_feedback is not None:
        return (
            f"A reviewer compared your previous SQL and its result against the "
            f"question and rejected it:\n{critic_feedback}\n\n"
            f"Previous SQL:\n{prior_sql}\n\n"
            "Write SQL that actually answers what was asked, addressing this "
            "specific mismatch."
        )
    return None


def build(model_client: ModelClient) -> Callable[[AgentState], dict]:
    def draft_sql(state: AgentState) -> dict:
        parts = [f"Question: {task_question(state)}"]

        # Only when there is a previous SUCCESSFUL query above to transform --
        # `last_reusable_turn` skips failed and abstained turns, so this never
        # points the drafter at a query that did not work.
        context = context_of(state)
        if context is not None and context.last_reusable_turn is not None:
            parts.append(TRANSFORM_GUIDANCE)

        # Last, so a correction dominates: a retry's job is to fix the specific
        # failure, not to weigh it against conversational guidance.
        retry_note = _retry_instruction(state)
        if retry_note:
            parts.append(retry_note)

        reply = model_client.complete(
            "draft_sql",
            messages=node_messages(state, "draft_sql", "\n\n".join(parts)),
            schema=RESPONSE_SCHEMA,
        )
        sql = reply.payload.get("sql")
        return {
            "sql": sql.strip() if isinstance(sql, str) and sql.strip() else None,
            "validation_error": None,
            "execute_error": None,
            "critic_verdict": None,
            "critic_feedback": None,
            "total_model_calls": 1,
            "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens,
            "cache_creation_input_tokens": reply.cache_creation_input_tokens,
            "cache_read_input_tokens": reply.cache_read_input_tokens,
        }

    return draft_sql
