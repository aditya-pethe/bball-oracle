"""GraphAgent: the real LangGraph agent, behind the same `Agent` protocol as
`ZeroShotBaseline` (evals/harness/baseline.py).

Lives in the harness, not in `agent/`, for the same reason the baseline does --
it is an eval *subject*, not service code. `agent/tests/test_no_eval_dependency.py`
enforces the direction: `agent/` must never import `evals.*`, so all the wiring
that turns "a graph" into "something the harness can score" belongs here.

This is deliberately a thin adapter. `agent/graph.py` builds the real
`CompiledStateGraph`; this module's only job is to invoke it in-process with a
real `AnthropicModelClient` + `InternalQueryExecutor` and translate the final
`AgentState` into an `AgentEnvelope`.

All four token counters are real measurements. An earlier version of this file
hardcoded `cache_creation_input_tokens` to 0 because the graph never threaded it
through -- which showed up in the first two graph runs as "wrote 0" alongside a
95% cache hit rate, an impossible pair, and understated cost. Fixed in
agent/llm.py, agent/state.py and agent/nodes/*.py on 2026-08-07.
"""
from __future__ import annotations

import os
import time

from agent.execute import InternalQueryExecutor
from agent.graph import RetryLimits, build_graph
from agent.llm import AnthropicModelClient, ModelClient
from agent.models import DEFAULT_MODEL
from agent.state import new_state

from .envelope import AgentEnvelope


class GraphAgent:
    """Implements evals.harness.envelope.Agent by driving the real graph.

    `model_client` / `executor` are injection seams for tests (a scripted fake
    in place of a real Anthropic client / real HTTP executor) -- same pattern
    as `agent/tests/conftest.py`'s FakeModelClient/FakeExecutor, which drive
    the identical graph structure. Left unset, both default to the real
    implementations, reading credentials from the environment exactly as the
    deployed service does (agent/service.py's `get_model_client` /
    `get_executor`).

    `user_id` has no graph-internal default -- InternalQueryExecutor.execute()
    requires one for attribution in `/api/internal/query`, and there is no
    anonymous path (AGENTS.md). Falls back to the `EVAL_USER_ID` env var,
    mirroring `evals.harness.executors.InternalApiExecutor`'s convention, so
    both agents are configured the same way from evals/run.py.
    """

    def __init__(
        self,
        *,
        model_client: ModelClient | None = None,
        executor: InternalQueryExecutor | None = None,
        user_id: int | None = None,
        model: str = DEFAULT_MODEL,
        limits: RetryLimits | None = None,
    ) -> None:
        self._model_client = model_client or AnthropicModelClient()
        self._executor = executor or InternalQueryExecutor()

        raw_user = user_id if user_id is not None else os.environ.get("EVAL_USER_ID")
        if raw_user in (None, ""):
            raise RuntimeError(
                "GraphAgent needs EVAL_USER_ID (or an explicit user_id) to attribute "
                "queries through /api/internal/query -- there is no anonymous path."
            )
        self._user_id = int(raw_user)
        self._model = model
        self._graph = build_graph(self._model_client, self._executor, limits=limits or RetryLimits())

    @property
    def name(self) -> str:
        return f"graph-{self._model}"

    def answer(self, question: str) -> AgentEnvelope:
        started = time.monotonic()
        state = dict(new_state(question, self._user_id))

        try:
            final = self._graph.invoke(state)
        except Exception as err:  # noqa: BLE001 - surfaced as a scored miss, see below
            # Mirrors ZeroShotBaseline.answer()'s contract exactly: a node's
            # model call or HTTP call can raise (network, auth, a refused
            # request -- see agent/llm.py and agent/execute.py's docstrings on
            # what is reserved for raising vs. a structured ExecOutcome), and
            # LangGraph does not catch node exceptions on its own. One flaky
            # call must not abort the run, so it is scored as a miss instead.
            return AgentEnvelope(
                outcome="answer",
                summary="",
                error=f"graph invocation failed: {type(err).__name__}: {err}",
                total_ms=(time.monotonic() - started) * 1000,
                model=self._model,
            )

        return _envelope_from_state(final, model=self._model, total_ms=(time.monotonic() - started) * 1000)


def _tool_calls(final: dict) -> int:
    """How many times SQL was actually sent to /api/internal/query.

    `outcome == "answer"` is only reached via draft_sql -> execute (directly,
    or through summarize's retries-exhausted error path -- both still mean
    execute ran at least once); `clarify`/`decline` short-circuit before
    execute is ever reached (agent/graph.py's `after_classify`). From there,
    every rung-1-3 retry (`execute_retry_count`) and every critic-triggered
    retry (`critic_retry_count`) is one more redraft that routes back through
    execute exactly once (agent/graph.py's `after_execute` / `after_critic`),
    so the total is the initial call plus both counters.
    """
    if final.get("outcome") != "answer":
        return 0
    return 1 + final.get("execute_retry_count", 0) + final.get("critic_retry_count", 0)


def _envelope_from_state(final: dict, *, model: str, total_ms: float) -> AgentEnvelope:
    return AgentEnvelope(
        outcome=final.get("outcome") or "answer",
        summary=final.get("summary") or "",
        sql=final.get("sql"),
        result=final.get("result"),
        error=final.get("error"),
        node_timings=list(final.get("node_timings", [])),
        tool_calls=_tool_calls(final),
        input_tokens=final.get("input_tokens", 0),
        output_tokens=final.get("output_tokens", 0),
        cache_creation_input_tokens=final.get("cache_creation_input_tokens", 0),
        cache_read_input_tokens=final.get("cache_read_input_tokens", 0),
        total_ms=total_ms,
        model=model,
    )
