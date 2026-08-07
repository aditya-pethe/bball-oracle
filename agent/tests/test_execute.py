"""InternalQueryExecutor against the /api/internal/query contract this step
assumed (agent/execute.py's docstring). No network -- httpx.MockTransport
stands in for the endpoint, which is being built concurrently in this repo."""
from __future__ import annotations

import json

import httpx
import pytest

from agent.execute import InternalQueryExecutor


def _transport(handler):
    return httpx.MockTransport(handler)


def test_construction_never_raises_without_credentials(monkeypatch):
    monkeypatch.delenv("AGENT_API_BASE_URL", raising=False)
    monkeypatch.delenv("AGENT_SERVICE_TOKEN", raising=False)
    InternalQueryExecutor()  # must not raise


def test_execute_without_credentials_raises_at_call_time(monkeypatch):
    monkeypatch.delenv("AGENT_API_BASE_URL", raising=False)
    monkeypatch.delenv("AGENT_SERVICE_TOKEN", raising=False)
    executor = InternalQueryExecutor()
    with pytest.raises(RuntimeError, match="AGENT_API_BASE_URL"):
        executor.execute("SELECT 1", user_id=1)


def test_200_maps_to_ok_outcome():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/internal/query"
        assert request.headers["authorization"] == "Bearer test-token"
        body = json.loads(request.content)
        assert body == {"sql": "SELECT 1", "userId": 42}
        assert "source" not in body  # the endpoint sets source, never the caller
        return httpx.Response(
            200,
            json={
                "columns": ["n"],
                "rows": [[1]],
                "rowCount": 1,
                "truncated": False,
                "durationMs": 12.5,
            },
        )

    executor = InternalQueryExecutor(
        base_url="http://agent-internal.test",
        token="test-token",
        transport=_transport(handler),
    )
    outcome = executor.execute("SELECT 1", user_id=42)
    assert outcome.ok
    assert outcome.status == "ok"
    assert outcome.result.columns == ("n",)
    assert outcome.result.rows == ((1,),)
    assert outcome.result.row_count == 1
    assert outcome.duration_ms == 12.5


def test_400_with_duration_is_a_query_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "relation does not exist", "durationMs": 3.0})

    executor = InternalQueryExecutor(
        base_url="http://agent-internal.test", token="t", transport=_transport(handler)
    )
    outcome = executor.execute("SELECT * FROM nope", user_id=1)
    assert not outcome.ok
    assert outcome.status == "error"
    assert "does not exist" in outcome.error


def test_400_without_duration_is_validation_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "multiple statements"})

    executor = InternalQueryExecutor(
        base_url="http://agent-internal.test", token="t", transport=_transport(handler)
    )
    outcome = executor.execute("SELECT 1; SELECT 2", user_id=1)
    assert outcome.status == "validation_rejected"


def test_429_maps_to_rate_limited():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    executor = InternalQueryExecutor(
        base_url="http://agent-internal.test", token="t", transport=_transport(handler)
    )
    outcome = executor.execute("SELECT 1", user_id=1)
    assert outcome.status == "rate_limited"
    assert not outcome.ok


def test_504_maps_to_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(504, json={"error": "statement timeout"})

    executor = InternalQueryExecutor(
        base_url="http://agent-internal.test", token="t", transport=_transport(handler)
    )
    outcome = executor.execute("SELECT 1", user_id=1)
    assert outcome.status == "timeout"


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_raises_rather_than_scoring_as_a_miss(status):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "unauthorized"})

    executor = InternalQueryExecutor(
        base_url="http://agent-internal.test", token="wrong", transport=_transport(handler)
    )
    with pytest.raises(RuntimeError, match="configuration failure"):
        executor.execute("SELECT 1", user_id=1)
