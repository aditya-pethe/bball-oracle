"""The service-token gate on POST /agent.

This is the Python half of AGENTS.md's "no anonymous execution path". The Next
proxy gates the browser, but the service is directly reachable on its own URL,
so the gate has to live here too -- these tests are the enforcement of that,
not a formality.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent.execute import ExecOutcome, ExecResult
from agent.service import app, get_executor, get_model_client
from agent.tests.conftest import FakeExecutor, FakeModelClient

TOKEN = "test-service-token-abc123"
BODY = {"question": "How many shots?", "user_id": 1}


def _fakes() -> tuple[FakeModelClient, FakeExecutor]:
    model = FakeModelClient(
        scripts={
            "classify": [{"verdict": "answerable", "clarify_question": None, "decline_reason": None}],
            "draft_sql": [{"sql": "SELECT COUNT(*) FROM nba.shot_detail"}],
            "critic": [{"verdict": "ok", "feedback": ""}],
            "summarize": [{"summary": "218701 shots."}],
        }
    )
    executor = FakeExecutor(
        outcomes=[ExecOutcome(status="ok", result=ExecResult(columns=("n",), rows=((218701,),)))]
    )
    return model, executor


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _client(monkeypatch, token: str | None = TOKEN):
    if token is None:
        monkeypatch.delenv("AGENT_SERVICE_TOKEN", raising=False)
    else:
        monkeypatch.setenv("AGENT_SERVICE_TOKEN", token)
    model, executor = _fakes()
    app.dependency_overrides[get_model_client] = lambda: model
    app.dependency_overrides[get_executor] = lambda: executor
    return TestClient(app), model, executor


def test_health_needs_no_token(monkeypatch):
    monkeypatch.delenv("AGENT_SERVICE_TOKEN", raising=False)
    assert TestClient(app).get("/health").status_code == 200


def test_valid_token_is_accepted(monkeypatch):
    client, _, _ = _client(monkeypatch)
    r = client.post("/agent", json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200


def test_bearer_scheme_is_case_insensitive(monkeypatch):
    client, _, _ = _client(monkeypatch)
    r = client.post("/agent", json=BODY, headers={"Authorization": f"bearer {TOKEN}"})
    assert r.status_code == 200


def test_missing_authorization_header_is_rejected(monkeypatch):
    client, _, _ = _client(monkeypatch)
    assert client.post("/agent", json=BODY).status_code == 401


@pytest.mark.parametrize(
    "header",
    [
        "",
        "Bearer",
        "Bearer ",
        f"Bearer {TOKEN}x",
        f"Bearer {TOKEN[:-1]}",
        TOKEN,               # no scheme
        f"Basic {TOKEN}",    # wrong scheme
        "Bearer wrong-token",
    ],
)
def test_bad_authorization_headers_are_rejected(monkeypatch, header):
    client, _, _ = _client(monkeypatch)
    r = client.post("/agent", json=BODY, headers={"Authorization": header})
    assert r.status_code == 401


def test_unset_token_fails_closed(monkeypatch):
    """A deploy that forgets the env var must be unreachable, not open."""
    client, _, _ = _client(monkeypatch, token=None)
    assert client.post("/agent", json=BODY).status_code == 503
    # An empty presented token must not match an unset expected one.
    assert client.post(
        "/agent", json=BODY, headers={"Authorization": "Bearer "}
    ).status_code == 503


def test_empty_token_env_fails_closed(monkeypatch):
    client, _, _ = _client(monkeypatch, token="")
    assert client.post("/agent", json=BODY).status_code == 503


def test_rejected_request_never_reaches_the_graph(monkeypatch):
    """The gate runs before anything is spent or executed.

    A 401 that still called the model would burn budget; one that still called
    the executor would have run SQL under an attacker-chosen user_id, which is
    the entire threat this gate exists to close.
    """
    client, model, executor = _client(monkeypatch)
    client.post("/agent", json={"question": "q", "user_id": 999})
    assert model.calls == []
    assert executor.calls == []
