"""summarize -- produces the envelope's `outcome`/`summary` for every path that
reaches it (a successful run, or the correction ladder exhausted its budget).

Unlike critic (which only needs shape + a peek to judge a match), summarize is
the user-facing explanation of what the query computed, so it reads the actual
result -- up to `_MAX_SUMMARY_ROWS` rows -- not just shape. Answer sets here
should generally be small already (the shared RULES section pushes draft_sql
toward LIMIT 1 / a stated top-N), so the cap is a safety net against a
pathological case, not the normal path.

Retries-exhausted path spends no model call, per this step's explicit
requirement (kept from the stub, verified by
test_retry_is_bounded_and_terminates_with_an_error in agent/tests/test_graph.py):
narrating an empty/failed result would burn tokens explaining nothing useful,
when the error string itself already says what happened. A zero-row result
that *did* survive to summarize (retries exhausted at rung 3, but a result
object is still present, just empty) is treated as a legitimate answer worth
narrating ("no players met this threshold"), not a system error -- only a
`result is None` alongside an unresolved error takes the no-call path.

`outcome` is always "answer" here: `clarify`/`decline` are separate terminal
nodes that bypass summarize entirely (agent/graph.py).
"""
from __future__ import annotations

from typing import Callable

from ..execute import ExecResult
from ..llm import ModelClient
from ..state import AgentState

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One or two sentences on what the query computes, "
                           "citing the actual answer from the result below (the "
                           "number, the leader, the top row) rather than just "
                           "restating the SQL. Carry forward any threshold or "
                           "definition noted in a SQL comment.",
        }
    },
    "required": ["summary"],
    "additionalProperties": False,
}

_MAX_SUMMARY_ROWS = 20


def _result_for_prompt(result: ExecResult | None) -> str:
    if result is None:
        return "no result"
    shown = result.rows[:_MAX_SUMMARY_ROWS]
    lines = [f"columns={result.columns!r}, {result.row_count} row(s) total", f"rows: {shown!r}"]
    remaining = result.row_count - len(shown)
    if remaining > 0:
        lines.append(f"({remaining} more row(s) not shown)")
    return "\n".join(lines)


def build(model_client: ModelClient) -> Callable[[AgentState], dict]:
    def summarize(state: AgentState) -> dict:
        # Retries exhausted with nothing usable to show: report the failure
        # rather than spending a model call narrating an empty result.
        unresolved_error = state.get("execute_error") or state.get("validation_error")
        if unresolved_error and state.get("result") is None:
            return {"outcome": "answer", "error": unresolved_error}

        content = (
            f"Question: {state['question']}\n\n"
            f"SQL:\n{state.get('sql')}\n\n"
            f"Result: {_result_for_prompt(state.get('result'))}"
        )
        reply = model_client.complete(
            "summarize",
            messages=[{"role": "user", "content": content}],
            schema=RESPONSE_SCHEMA,
        )
        return {
            "outcome": "answer",
            "summary": reply.payload.get("summary", ""),
            "total_model_calls": 1,
            "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens,
            "cache_creation_input_tokens": reply.cache_creation_input_tokens,
            "cache_read_input_tokens": reply.cache_read_input_tokens,
        }

    return summarize
