"""FastAPI service tests. No credentials, no network -- /agent's dependencies
are swapped for the same fakes used in test_graph.py via
app.dependency_overrides, and /health is asserted to need nothing at all.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agent.execute import ExecOutcome, ExecResult
from agent.service import app, get_executor, get_model_client
from agent.tests.conftest import FakeExecutor, FakeModelClient


SERVICE_TOKEN = "test-service-token"


@pytest.fixture(autouse=True)
def _no_credentials(monkeypatch):
    """No MODEL credentials -- this step's verification requires the suite pass
    without them. AGENT_SERVICE_TOKEN is the exception and is set: it is not a
    credential the service spends, it is the gate that stops /agent being an
    anonymous execution path (see test_service_auth.py). Absent it, every
    request here would 503 and these tests would assert nothing."""
    for var in ("ANTHROPIC_API_KEY", "AGENT_API_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGENT_SERVICE_TOKEN", SERVICE_TOKEN)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def test_health_responds_without_any_credentials():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_agent_endpoint_streams_node_events_then_done():
    model = FakeModelClient(
        scripts={
            "classify": [{"verdict": "answerable", "clarify_question": None, "decline_reason": None}],
            "draft_sql": [{"sql": "SELECT COUNT(*) FROM nba.shot_detail"}],
            "critic": [{"verdict": "ok", "feedback": ""}],
            "summarize": [{"summary": "705 shots."}],
        }
    )
    executor = FakeExecutor(
        outcomes=[ExecOutcome(status="ok", result=ExecResult(columns=("n",), rows=((705,),)))]
    )
    app.dependency_overrides[get_model_client] = lambda: model
    app.dependency_overrides[get_executor] = lambda: executor

    client = TestClient(app)
    with client.stream(
        "POST",
        "/agent",
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        json={"question": "How many shots?", "user_id": 1, "conversation_id": None},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    events = [block for block in body.split("\n\n") if block.strip()]
    assert len(events) >= 2  # at least one "node" event plus the terminal "done"
    assert events[0].startswith("event: node")
    assert events[-1].startswith("event: done")

    done_data = json.loads(events[-1].split("data: ", 1)[1])
    assert done_data["outcome"] == "answer"
    assert done_data["summary"] == "705 shots."
    assert done_data["sql"] == "SELECT COUNT(*) FROM nba.shot_detail"
    assert done_data["error"] is None
    assert done_data["result"] == {
        "columns": ["n"],
        "rows": [[705]],
        "rowCount": 1,
        "truncated": False,
    }
    assert {t["node"] for t in done_data["node_timings"]} == {
        "classify",
        "draft_sql",
        "execute",
        "critic",
        "summarize",
    }
    assert done_data["input_tokens"] == 40


def test_agent_endpoint_clarify_short_circuit_streams_only_classify_and_done():
    model = FakeModelClient(
        scripts={"classify": [{"verdict": "ambiguous", "clarify_question": "Which season?", "decline_reason": None}]}
    )
    executor = FakeExecutor(outcomes=[])
    app.dependency_overrides[get_model_client] = lambda: model
    app.dependency_overrides[get_executor] = lambda: executor

    client = TestClient(app)
    with client.stream(
        "POST", "/agent", json={"question": "How many shots this season?", "user_id": 1},
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    ) as response:
        body = "".join(response.iter_text())

    events = [block for block in body.split("\n\n") if block.strip()]
    done_data = json.loads(events[-1].split("data: ", 1)[1])
    assert done_data["outcome"] == "clarify"
    assert done_data["summary"] == "Which season?"
    assert done_data["sql"] is None
    assert executor.calls == []


# ---------------------------------------------------------------------------
# Heartbeats (.agents/p5_regression_report.md, 2026-08-10)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_quiet_source_produces_keepalives(monkeypatch):
    """A node emits nothing until it finishes; draft_sql has been measured at 25s.
    Without a heartbeat that window is silent on the wire."""
    import asyncio

    from agent import service
    from agent.service import _HEARTBEAT, _with_heartbeats

    monkeypatch.setattr(service, "HEARTBEAT_SECONDS", 0.01)

    async def slow():
        await asyncio.sleep(0.05)
        yield "event: done\ndata: {}\n\n"

    chunks = [chunk async for chunk in _with_heartbeats(slow())]

    assert _HEARTBEAT in chunks
    assert chunks[-1] == "event: done\ndata: {}\n\n"


@pytest.mark.anyio
async def test_a_fast_source_is_passed_through_untouched():
    from agent.service import _HEARTBEAT, _with_heartbeats

    async def fast():
        yield "event: node\ndata: {}\n\n"
        yield "event: done\ndata: {}\n\n"

    chunks = [chunk async for chunk in _with_heartbeats(fast())]
    assert chunks == ["event: node\ndata: {}\n\n", "event: done\ndata: {}\n\n"]
    assert _HEARTBEAT not in chunks


@pytest.mark.anyio
async def test_an_error_in_the_source_still_surfaces():
    from agent.service import _with_heartbeats

    async def boom():
        yield "event: node\ndata: {}\n\n"
        raise RuntimeError("graph exploded")

    with pytest.raises(RuntimeError, match="graph exploded"):
        [chunk async for chunk in _with_heartbeats(boom())]


def test_a_heartbeat_is_ignored_by_the_sse_readers():
    """Both consumers of this stream skip a block with no `data:` line —
    web/lib/agent-client.ts's parseSseBlock and the proxy's consume()."""
    from agent.service import _HEARTBEAT

    block = _HEARTBEAT.split("\n\n")[0]
    assert not any(line.startswith("data:") for line in block.split("\n"))
    assert not any(line.startswith("event:") for line in block.split("\n"))
