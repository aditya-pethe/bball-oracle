"""The LangGraph agent.

    classify -> (clarify | decline | draft_sql) -> execute -> critic -> summarize
                          ^                           |         |
                          |                           |         |
                          +------- bounded retry -----+---------+

There is deliberately NO separate `validate` node, despite the plan's diagram
showing one (.agents/p4_agent.md, corrected 2026-08-06). Validation already
happens inside `/api/internal/query`: `runUserSql` calls the libpg-query
validator before it executes anything, and returns a rejection as a structured
400 (no `durationMs`, which is how it is told apart from a SQL error). Adding a
Python validator here would mean two validators, in two languages, that can
disagree -- and the asymmetry is bad in both directions: a stricter local check
refuses queries the server would have run, while a looser one only buys a
wasted round trip. That is precisely what principle 3 ("one enforcement path")
exists to prevent, so the execute node carries rungs 1 AND 2 of the correction
ladder, distinguishing them via `validation_error` vs `execute_error` so the
retry prompt can say the right thing.

(.agents/p4_agent.md "Agent loop: explicit LangGraph"). Nodes are STUBS
(agent/nodes/*.py) -- step 6 fills in real prompts and logic. This module owns
the shape that step 6 cannot get wrong by accident: the edges, the
bounded-retry caps, and per-node latency instrumentation.

Nodes close over their dependencies (an injected `ModelClient` /
`InternalQueryExecutor`) via each module's `build(...)` factory, so tests
substitute fakes without touching the graph structure at all -- `build_graph`
is the one function that wires dependency to node.

LangSmith tracing (.agents/p4_agent.md "Resolved 2026-08-06" #1) needs no code
here: langchain-core (a langgraph dependency) auto-installs a tracer whenever
LANGSMITH_TRACING=true and LANGSMITH_API_KEY are set in the environment, and is
a silent no-op otherwise. This file must not disable or gate that -- it is
what keeps tracing "entirely optional" per this step's hard constraint.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from .envelope import NodeTiming

from .execute import InternalQueryExecutor
from .llm import ModelClient
from .nodes import classify as classify_node
from .nodes import clarify as clarify_node
from .nodes import critic as critic_node
from .nodes import decline as decline_node
from .nodes import draft_sql as draft_sql_node
from .nodes import execute as execute_node
from .nodes import summarize as summarize_node
from .state import AgentState


@dataclass(frozen=True)
class RetryLimits:
    """Bounded retry caps (Correction ladder, .agents/p4_agent.md).

    The plan requires retry to be bounded and never the headline metric but
    does not specify numbers -- these are this step's judgment call. Each
    field is a cap on *total attempts at that rung*, so a value of 2 permits
    exactly one retry. The cheap rungs (1-3, all reported by execute) get a
    couple of tries; the expensive one (4, critic -- "1 extra model call" per
    the plan) is capped at exactly that: one retry, never more. A combined cap
    sits underneath both so they can't stack into an unbounded run. Tune during
    step 8's model x effort sweep, once there are real numbers on whether
    retrying earns its cost.

    There is no `validate` limit: rungs 1-3 (SQL error, validator rejection,
    degenerate result) all surface from the single execute node and share its
    budget. See the module docstring for why validation is not its own node.
    """

    execute: int = 2
    critic: int = 2
    total_model_calls: int = 6


def _timed(name: str, fn: Callable[[AgentState], dict]) -> Callable[[AgentState], dict]:
    """Wraps any node with wall-clock timing, so nodes don't each reimplement
    it and the envelope's per-node p50/p95 metric has real data from every
    run, stubs included."""

    def wrapper(state: AgentState) -> dict:
        started = time.monotonic()
        update = dict(fn(state))
        update["node_timings"] = [NodeTiming(name, (time.monotonic() - started) * 1000)]
        return update

    return wrapper


def build_graph(
    model_client: ModelClient,
    executor: InternalQueryExecutor,
    *,
    limits: RetryLimits | None = None,
) -> CompiledStateGraph:
    limits = limits or RetryLimits()
    graph = StateGraph(AgentState)

    graph.add_node("classify", _timed("classify", classify_node.build(model_client)))
    graph.add_node("clarify", _timed("clarify", clarify_node.build()))
    graph.add_node("decline", _timed("decline", decline_node.build()))
    graph.add_node("draft_sql", _timed("draft_sql", draft_sql_node.build(model_client)))
    graph.add_node("execute", _timed("execute", execute_node.build(executor)))
    graph.add_node("critic", _timed("critic", critic_node.build(model_client)))
    graph.add_node("summarize", _timed("summarize", summarize_node.build(model_client)))

    graph.add_edge(START, "classify")

    def after_classify(state: AgentState) -> str:
        outcome = state.get("outcome")
        if outcome == "clarify":
            return "clarify"
        if outcome == "decline":
            return "decline"
        return "draft_sql"

    graph.add_conditional_edges("classify", after_classify, ["clarify", "decline", "draft_sql"])
    graph.add_edge("clarify", END)
    graph.add_edge("decline", END)
    graph.add_edge("draft_sql", "execute")

    def _budget_exhausted(state: AgentState) -> bool:
        return state.get("total_model_calls", 0) >= limits.total_model_calls

    def after_execute(state: AgentState) -> str:
        # Fatal first: a caller error (unknown user, malformed request) is not a
        # rung on the ladder, and retrying it burns model calls on a problem the
        # model cannot see, let alone fix.
        if state.get("fatal_error"):
            return "summarize"

        # Rungs 1-3 share one edge because they share one repair: redraft. They
        # stay distinguishable on the state (`validation_error` vs
        # `execute_error`) because they do NOT share a repair *prompt* -- "your
        # SQL had a syntax error" and "your SQL was rejected as not a single
        # SELECT" are different instructions to the drafter.
        if state.get("execute_error") is None and state.get("validation_error") is None:
            return "critic"
        if _budget_exhausted(state) or state.get("execute_retry_count", 0) >= limits.execute:
            return "summarize"
        return "draft_sql"

    graph.add_conditional_edges("execute", after_execute, ["critic", "draft_sql", "summarize"])

    def after_critic(state: AgentState) -> str:
        if state.get("critic_verdict") != "reject":
            return "summarize"
        if _budget_exhausted(state) or state.get("critic_retry_count", 0) >= limits.critic:
            return "summarize"
        return "draft_sql"

    graph.add_conditional_edges("critic", after_critic, ["summarize", "draft_sql"])
    graph.add_edge("summarize", END)

    return graph.compile()
