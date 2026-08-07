"""The graph's final state, once shaped into evals.harness.envelope.AgentEnvelope
(the same conversion agent/service.py's `_envelope()` does for the SSE `done`
event), must actually satisfy that contract -- it's what the eval harness
(evals/harness/runner.py) and the UI both consume. Covers all three outcomes:
answer, clarify, decline.
"""
from __future__ import annotations

from agent.execute import ExecOutcome, ExecResult as AgentExecResult
from agent.graph import build_graph
from agent.state import AgentState, new_state
from agent.tests.conftest import FakeExecutor, FakeModelClient
from evals.harness.envelope import AgentEnvelope, ExecResult as EnvExecResult


def _to_envelope(state: AgentState) -> AgentEnvelope:
    """Mirrors agent/service.py's `_envelope()`, but into the real dataclass
    rather than a JSON-able dict, so it can be checked against the Agent
    protocol's actual return type."""
    result = state.get("result")
    env_result = (
        EnvExecResult(columns=result.columns, rows=result.rows, truncated=result.truncated)
        if result is not None
        else None
    )
    return AgentEnvelope(
        outcome=state["outcome"],
        summary=state.get("summary") or "",
        sql=state.get("sql"),
        result=env_result,
        error=state.get("error"),
        node_timings=state.get("node_timings", []),
        tool_calls=1 if state.get("sql") else 0,
        input_tokens=state.get("input_tokens", 0),
        output_tokens=state.get("output_tokens", 0),
        cache_read_input_tokens=state.get("cache_read_input_tokens", 0),
    )


def test_answer_outcome_produces_a_well_formed_envelope():
    model = FakeModelClient(
        scripts={
            "classify": [{"verdict": "answerable", "clarify_question": None, "decline_reason": None}],
            "draft_sql": [{"sql": "SELECT COUNT(*) FROM nba.shot_detail"}],
            "critic": [{"verdict": "ok", "feedback": ""}],
            "summarize": [{"summary": "705 shots were attempted."}],
        }
    )
    executor = FakeExecutor(
        outcomes=[ExecOutcome(status="ok", result=AgentExecResult(columns=("n",), rows=((705,),)))]
    )
    final = build_graph(model, executor).invoke(new_state("How many shots?", user_id=1))
    envelope = _to_envelope(final)

    assert isinstance(envelope, AgentEnvelope)
    assert envelope.outcome == "answer"
    assert envelope.summary == "705 shots were attempted."
    assert envelope.sql == "SELECT COUNT(*) FROM nba.shot_detail"
    assert envelope.error is None
    assert envelope.result is not None
    assert envelope.result.rows == ((705,),)
    assert envelope.result.row_count == 1
    assert envelope.tool_calls == 1
    assert envelope.total_input_tokens > 0
    assert {t.node for t in envelope.node_timings} == {
        "classify",
        "draft_sql",
        "execute",
        "critic",
        "summarize",
    }


def test_clarify_outcome_produces_a_well_formed_envelope():
    model = FakeModelClient(
        scripts={
            "classify": [
                {
                    "verdict": "ambiguous",
                    "clarify_question": "Best by efficiency or by volume?",
                    "decline_reason": None,
                }
            ]
        }
    )
    executor = FakeExecutor(outcomes=[])
    final = build_graph(model, executor).invoke(new_state("Who is the best shooter?", user_id=1))
    envelope = _to_envelope(final)

    assert isinstance(envelope, AgentEnvelope)
    assert envelope.outcome == "clarify"
    assert envelope.summary == "Best by efficiency or by volume?"
    assert envelope.sql is None
    assert envelope.result is None
    assert envelope.tool_calls == 0


def test_decline_outcome_produces_a_well_formed_envelope():
    model = FakeModelClient(
        scripts={
            "classify": [
                {"verdict": "unanswerable", "clarify_question": None, "decline_reason": "No awards data."}
            ]
        }
    )
    executor = FakeExecutor(outcomes=[])
    final = build_graph(model, executor).invoke(new_state("Who won MVP?", user_id=1))
    envelope = _to_envelope(final)

    assert isinstance(envelope, AgentEnvelope)
    assert envelope.outcome == "decline"
    assert envelope.summary == "No awards data."
    assert envelope.sql is None
    assert envelope.result is None
    assert envelope.tool_calls == 0


def test_exhausted_retry_produces_an_answer_envelope_with_an_error_and_no_summary_call():
    from agent.graph import RetryLimits

    limits = RetryLimits(execute=1, critic=1, total_model_calls=10)
    model = FakeModelClient(
        scripts={
            "classify": [{"verdict": "answerable", "clarify_question": None, "decline_reason": None}],
            "draft_sql": [{"sql": "UPDATE nba.shot_detail SET x = 1"}],
        }
    )
    executor = FakeExecutor(
        outcomes=[ExecOutcome(status="validation_rejected", error="only SELECT statements are allowed")]
    )
    final = build_graph(model, executor, limits=limits).invoke(new_state("q", user_id=1))
    envelope = _to_envelope(final)

    assert isinstance(envelope, AgentEnvelope)
    assert envelope.outcome == "answer"
    assert envelope.error is not None
    assert "summarize" not in model.calls
