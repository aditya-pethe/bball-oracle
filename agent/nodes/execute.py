"""execute -- runs the drafted SQL via /api/internal/query (agent/execute.py).

This node carries rungs 1-3 of the Correction ladder, because all three arrive
from the same HTTP call:

  rung 1  SQL syntax / unknown column   -> 400 WITH durationMs (it reached Postgres)
  rung 2  validator rejection           -> 400 WITHOUT durationMs (rejected before execution)
  rung 3  degenerate result             -> 200, but zero rows (or one row where a
                                           ranking was asked for)

They route back to draft_sql on the same edge but are kept apart on the state,
because the repair *prompt* differs: telling the drafter "your SQL had a syntax
error" when it was actually rejected for not being a single SELECT sends it to
fix the wrong thing.

There is no separate validate node -- see agent/graph.py's module docstring for
why (one enforcement path; the real libpg-query validator already runs inside
/api/internal/query and its rejection is what rung 2 reads).

STUB: the rung-3 check is deliberately crude -- it catches zero rows but not
"1 row when a ranking was asked for", because that needs the question's intent
and this stub does not look at it. Step 6 owns the real thresholds.
"""
from __future__ import annotations

from typing import Callable

from ..execute import InternalQueryExecutor
from ..state import AgentState


def build(executor: InternalQueryExecutor) -> Callable[[AgentState], dict]:
    def execute(state: AgentState) -> dict:
        sql = state.get("sql") or ""
        outcome = executor.execute(
            sql, state["user_id"], state.get("conversation_message_id")
        )

        if outcome.status == "config_error":
            # Not a rung at all. The request was wrong, not the SQL, so this
            # goes straight to summarize instead of consuming redrafts.
            return {
                "fatal_error": outcome.error,
                "validation_error": None,
                "execute_error": outcome.error,
                "result": None,
            }

        if outcome.status == "validation_rejected":
            # Rung 2. The server's libpg-query validator refused it; nothing ran,
            # so there is no result and no duration.
            return {
                "validation_error": outcome.error or "rejected by the SQL validator",
                "execute_error": None,
                "execute_retry_count": 1,
                "result": None,
            }

        if not outcome.ok:
            # Rung 1 (and timeouts, which are also worth a redraft -- usually a
            # missing filter rather than a genuinely expensive question).
            return {
                "validation_error": None,
                "execute_error": outcome.error or outcome.status,
                "execute_retry_count": 1,
                "result": None,
            }

        if outcome.result is not None and outcome.result.row_count == 0:
            # Rung 3: degenerate result. The result is kept (so summarize can tell
            # "ran but empty" apart from "never ran") while still routing back.
            return {
                "validation_error": None,
                "execute_error": "query returned zero rows",
                "execute_retry_count": 1,
                "result": outcome.result,
            }

        return {"validation_error": None, "execute_error": None, "result": outcome.result}

    return execute
