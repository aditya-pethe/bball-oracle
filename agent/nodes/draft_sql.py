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

from ..context import context_of, node_messages, task_question
from ..llm import ModelClient
from ..prompts import load, render
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
        return render("retry_validation", error=validation_error, sql=prior_sql)
    # Rungs 1 and 3 both surface as execute_error, distinguished by message.
    if execute_error is not None:
        if execute_error == "query returned zero rows":
            return render("retry_zero_rows", sql=prior_sql)
        return render("retry_execute", error=execute_error, sql=prior_sql)
    # Rung 4: critic rejection.
    if critic_feedback is not None:
        return render("retry_critic", feedback=critic_feedback, sql=prior_sql)
    return None


def build(model_client: ModelClient) -> Callable[[AgentState], dict]:
    def draft_sql(state: AgentState) -> dict:
        # Only when there is a previous SUCCESSFUL query above to transform --
        # `last_reusable_turn` skips failed and abstained turns, so this never
        # points the drafter at a query that did not work.
        context = context_of(state)
        transform_guidance = ""
        if context is not None and context.last_reusable_turn is not None:
            transform_guidance = f"\n\n{load('transform_guidance')}"

        # Last, so a correction dominates: a retry's job is to fix the specific
        # failure, not to weigh it against conversational guidance.
        retry_note = _retry_instruction(state)
        retry_instruction = f"\n\n{retry_note}" if retry_note else ""

        content = render(
            "draft_sql",
            question=task_question(state),
            transform_guidance=transform_guidance,
            retry_instruction=retry_instruction,
        )

        reply = model_client.complete(
            "draft_sql",
            messages=node_messages(state, "draft_sql", content),
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
