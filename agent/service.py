"""FastAPI service exposing the LangGraph agent (.agents/p4_agent.md step 5).

POST /agent streams the graph's per-node progress as SSE, ending with a `done`
event carrying the final envelope (shape matches evals/harness/envelope.py's
AgentEnvelope: outcome / summary / sql / result / error / node_timings /
tool_calls / token counts). GET /health is unauthenticated and touches neither
the model client nor the executor, so it responds even when neither
ANTHROPIC_API_KEY nor AGENT_SERVICE_TOKEN is configured.

Per the plan's security decision (.agents/p4_agent.md "How the agent executes
SQL"), this process never holds a database DSN -- SQL execution goes through
agent/execute.py's HTTP call to /api/internal/query, nothing else.
"""
from __future__ import annotations

import hmac
import json
import os
from typing import Any, AsyncIterator, Iterator

import anyio
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from starlette.responses import StreamingResponse

from .context import ConversationContext
from .execute import ExecResult, InternalQueryExecutor
from .graph import build_graph
from .llm import AnthropicModelClient, ModelClient
from .state import new_state

app = FastAPI(title="bball-oracle agent service")


class AgentRequest(BaseModel):
    question: str
    user_id: int
    conversation_id: str | None = None
    # The app.conversation_message row this turn is being written to. Threaded
    # down to /api/internal/query so query_log rows link back to the question
    # that caused them -- without it the audit trail has SQL but no question,
    # which is most of the signal when reviewing what the agent wrote.
    conversation_message_id: int | None = None
    # The bounded prior-turn window (.agents/p5_conversational_context.md). Read by
    # the Next.js proxy from `app.conversation_message` under a verified
    # `(user_id, conversation_id)` scope -- this service never fetches a conversation
    # and still holds no database credential. Optional so a single-turn caller (and
    # every existing Phase 4 client) needs no change.
    #
    # Not trusted as sent: ConversationContext re-applies its own bounds on
    # validation, so an oversized or malformed window is clamped here too.
    context: ConversationContext | None = None


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def require_service_token(authorization: str | None = Header(default=None)) -> None:
    """Gate on the shared secret the Next.js proxy presents.

    Without this the service is an anonymous execution path wearing a costume.
    It holds `AGENT_SERVICE_TOKEN`, and `/api/internal/query` takes the caller's
    word for `userId` because that endpoint's own gate is the service token it
    was handed. So an unauthenticated `POST /agent` on a public Fly URL lets
    anyone submit `{"question": ..., "user_id": <someone else>}` and have SQL
    executed and attributed to a user they chose -- consuming that user's rate
    limit and landing in their history. The browser-facing gate in the Next
    proxy does not help, because the service is reachable directly.

    AGENTS.md: "NEVER add an anonymous execution path, even for local testing
    convenience."

    Fails CLOSED: an unset or empty AGENT_SERVICE_TOKEN authenticates nobody,
    so a deploy that forgets the env var is unreachable rather than open. This
    mirrors sourceForToken() in web/app/api/internal/query/route.ts.
    """
    expected = os.environ.get("AGENT_SERVICE_TOKEN") or ""
    if not expected:
        raise HTTPException(status_code=503, detail="service token not configured")

    presented = ""
    if authorization:
        scheme, _, rest = authorization.partition(" ")
        if scheme.lower() == "bearer":
            presented = rest.strip()

    # compare_digest is constant-time and, unlike timingSafeEqual, tolerates a
    # length mismatch without raising.
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="invalid service token")


# FastAPI dependencies -- overridden in tests via app.dependency_overrides so
# a request never constructs a real Anthropic client or makes a real HTTP call.
def get_model_client() -> ModelClient:
    return AnthropicModelClient()


def get_executor() -> InternalQueryExecutor:
    return InternalQueryExecutor()


_EXHAUSTED = object()


async def _sync_to_async_iter(sync_iter: Iterator[Any]) -> AsyncIterator[Any]:
    """Bridges a sync generator (LangGraph's `.stream()`) into an async one by
    running each `next()` call in a worker thread, so the event loop is never
    blocked by a node's (currently synchronous) model or HTTP call.

    Uses a sentinel rather than catching StopIteration across the thread
    boundary -- a StopIteration raised inside the worker thread's callable
    surfaces as "coroutine raised StopIteration" (PEP 479) once anyio
    re-raises it on this side, not a catchable StopIteration here.
    """
    it = iter(sync_iter)
    while True:
        item = await anyio.to_thread.run_sync(next, it, _EXHAUSTED)
        if item is _EXHAUSTED:
            return
        yield item


def _result_to_dict(result: ExecResult | None) -> dict | None:
    if result is None:
        return None
    return {
        "columns": list(result.columns),
        "rows": [list(row) for row in result.rows],
        "rowCount": result.row_count,
        "truncated": result.truncated,
    }


def _jsonable_update(update: dict) -> dict:
    out: dict = {}
    for key, value in update.items():
        if key == "node_timings":
            out[key] = [{"node": t.node, "duration_ms": t.duration_ms} for t in value]
        elif key == "result":
            out[key] = _result_to_dict(value)
        else:
            out[key] = value
    return out


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _envelope(state: dict) -> dict:
    return {
        "outcome": state.get("outcome"),
        "summary": state.get("summary"),
        "sql": state.get("sql"),
        "result": _result_to_dict(state.get("result")),
        "error": state.get("error"),
        "node_timings": [
            {"node": t.node, "duration_ms": t.duration_ms} for t in state.get("node_timings", [])
        ],
        "tool_calls": 1 if state.get("sql") else 0,
        "input_tokens": state.get("input_tokens", 0),
        "output_tokens": state.get("output_tokens", 0),
        "cache_creation_input_tokens": state.get("cache_creation_input_tokens", 0),
        "cache_read_input_tokens": state.get("cache_read_input_tokens", 0),
    }


# A node event is only emitted once its node FINISHES, and draft_sql has been
# measured at 25s in production. For that whole window the connection carries no
# bytes, which makes a slow turn indistinguishable from a dead one -- to any
# buffering proxy between here and the browser, and to the user
# (.agents/p5_regression_report.md, 2026-08-10). An SSE comment costs nothing and
# is ignored by both readers of this stream: web/lib/agent-client.ts's
# parseSseBlock and the proxy's own consume() both require a `data:` line and skip
# a block without one.
# 5s, not 10: measured node durations are 2-25s, and at 10s a typical turn emits
# no heartbeat at all, which defeats the point. At 5s any node slower than that --
# which is most draft_sql calls -- produces a steady liveness signal. The cost is
# a 13-byte comment line.
HEARTBEAT_SECONDS = 5.0
_HEARTBEAT = ": keepalive\n\n"


async def _with_heartbeats(source: AsyncIterator[str]) -> AsyncIterator[str]:
    """Yields everything `source` produces, plus a keepalive whenever it goes
    quiet for longer than HEARTBEAT_SECONDS."""
    import asyncio

    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    _DONE = object()

    async def pump() -> None:
        try:
            async for item in source:
                await queue.put(item)
        except Exception as err:  # noqa: BLE001 - re-raised on the consumer side
            await queue.put(err)
        else:
            await queue.put(_DONE)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield _HEARTBEAT
                continue
            if item is _DONE:
                return
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        task.cancel()


async def _stream_agent(
    req: AgentRequest, model_client: ModelClient, executor: InternalQueryExecutor
) -> AsyncIterator[str]:
    graph = build_graph(model_client, executor)
    state = dict(
        new_state(
            req.question,
            req.user_id,
            req.conversation_id,
            conversation_message_id=req.conversation_message_id,
            conversation_context=req.context,
        )
    )

    sync_stream = graph.stream(state, stream_mode="updates")
    async for chunk in _sync_to_async_iter(sync_stream):
        for node_name, update in chunk.items():
            for key, value in update.items():
                if key == "node_timings":
                    state["node_timings"] = state.get("node_timings", []) + value
                elif key in (
                    "input_tokens",
                    "output_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                    "total_model_calls",
                    "execute_retry_count",
                    "critic_retry_count",
                ):
                    state[key] = state.get(key, 0) + value
                else:
                    state[key] = value
            yield _sse("node", {"node": node_name, **_jsonable_update(update)})

    yield _sse("done", _envelope(state))


@app.post("/agent", dependencies=[Depends(require_service_token)])
async def agent_endpoint(
    req: AgentRequest,
    model_client: ModelClient = Depends(get_model_client),
    executor: InternalQueryExecutor = Depends(get_executor),
) -> StreamingResponse:
    return StreamingResponse(
        _with_heartbeats(_stream_agent(req, model_client, executor)),
        media_type="text/event-stream",
    )
