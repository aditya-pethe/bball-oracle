"""End-to-end graph tests: stubbed model + stubbed executor drive the real
LangGraph structure (edges, bounded retry) in agent/graph.py -- node logic is
a stub (step 6's job), but the shape being tested here is real.
"""
from __future__ import annotations

from agent.execute import ExecOutcome, ExecResult
from agent.graph import RetryLimits, build_graph
from agent.state import new_state
from agent.tests.conftest import FakeExecutor, FakeModelClient

QUESTION = "How many made shots did Luka Doncic have?"


def _happy_path_scripts() -> dict[str, list[dict]]:
    return {
        "classify": [{"verdict": "answerable", "clarify_question": None, "decline_reason": None}],
        "draft_sql": [{"sql": "SELECT COUNT(*) FROM nba.shot_detail"}],
        "critic": [{"verdict": "ok", "feedback": ""}],
        "summarize": [{"summary": "Luka made 705 shots."}],
    }


def test_happy_path_runs_every_node_exactly_once():
    model = FakeModelClient(scripts=_happy_path_scripts())
    executor = FakeExecutor(
        outcomes=[ExecOutcome(status="ok", result=ExecResult(columns=("n",), rows=((705,),)))]
    )
    graph = build_graph(model, executor)

    final = graph.invoke(new_state(QUESTION, user_id=1))

    assert final["outcome"] == "answer"
    assert final["summary"] == "Luka made 705 shots."
    assert final["sql"] == "SELECT COUNT(*) FROM nba.shot_detail"
    assert final["error"] is None
    assert model.calls == ["classify", "draft_sql", "critic", "summarize"]
    assert executor.calls == ["SELECT COUNT(*) FROM nba.shot_detail"]

    # Every node's timing was recorded -- the response envelope's per-node
    # latency metric needs this from every run, not just a happy accident.
    timed_nodes = {t.node for t in final["node_timings"]}
    assert timed_nodes == {"classify", "draft_sql", "execute", "critic", "summarize"}
    assert all(t.duration_ms >= 0 for t in final["node_timings"])

    # Token usage accumulated across the four model-calling nodes.
    assert final["input_tokens"] == 40  # 10 per call x 4 calls
    assert final["total_model_calls"] == 4


def test_classify_ambiguous_short_circuits_to_clarify_before_any_sql():
    model = FakeModelClient(
        scripts={"classify": [{"verdict": "ambiguous", "clarify_question": "Best by what measure?", "decline_reason": None}]}
    )
    executor = FakeExecutor(outcomes=[])
    graph = build_graph(model, executor)

    final = graph.invoke(new_state("Who is the best shooter?", user_id=1))

    assert final["outcome"] == "clarify"
    assert final["summary"] == "Best by what measure?"
    assert final["sql"] is None
    assert model.calls == ["classify"]
    assert executor.calls == []  # never reached -- the whole point of classify


def test_classify_unanswerable_short_circuits_to_decline():
    model = FakeModelClient(
        scripts={"classify": [{"verdict": "unanswerable", "clarify_question": None, "decline_reason": "No awards data."}]}
    )
    executor = FakeExecutor(outcomes=[])
    graph = build_graph(model, executor)

    final = graph.invoke(new_state("Who won MVP?", user_id=1))

    assert final["outcome"] == "decline"
    assert final["summary"] == "No awards data."
    assert executor.calls == []


def test_validator_rejection_retries_draft_sql_then_succeeds():
    """Rung 2: a validator rejection feeds back to draft_sql as structured
    signal and the corrected SQL runs clean -- the retry path this step's
    verification explicitly requires."""
    model = FakeModelClient(
        scripts={
            "classify": [{"verdict": "answerable", "clarify_question": None, "decline_reason": None}],
            "draft_sql": [
                {"sql": "UPDATE nba.shot_detail SET shot_made_flag = 1"},  # rejected: a write
                {"sql": "SELECT COUNT(*) FROM nba.shot_detail"},  # corrected
            ],
            "critic": [{"verdict": "ok", "feedback": ""}],
            "summarize": [{"summary": "705 shots."}],
        }
    )
    # Rung 2 now comes from the server: /api/internal/query runs the real
    # libpg-query validator and answers 400-without-durationMs. There is no
    # local validator to reject it first.
    executor = FakeExecutor(
        outcomes=[
            ExecOutcome(status="validation_rejected", error="only SELECT statements are allowed"),
            ExecOutcome(status="ok", result=ExecResult(columns=("n",), rows=((705,),))),
        ]
    )
    graph = build_graph(model, executor)

    final = graph.invoke(new_state(QUESTION, user_id=1))

    assert final["outcome"] == "answer"
    assert final["sql"] == "SELECT COUNT(*) FROM nba.shot_detail"
    assert final["execute_retry_count"] == 1
    # The rejection is kept distinct from a SQL error so the redraft prompt can
    # say the right thing; by the end it is cleared.
    assert final["validation_error"] is None
    # draft_sql ran twice (the retry), everything else once.
    assert model.calls == ["classify", "draft_sql", "draft_sql", "critic", "summarize"]
    # BOTH drafts reached the executor -- that is the point of having one
    # enforcement path: the write never executes, but it is the server that
    # refuses it, not a second validator living in this process.
    assert executor.calls == [
        "UPDATE nba.shot_detail SET shot_made_flag = 1",
        "SELECT COUNT(*) FROM nba.shot_detail",
    ]


def test_degenerate_result_retries_execute_then_succeeds():
    """Rung 3: a zero-row result is treated as a failure and retried."""
    model = FakeModelClient(
        scripts={
            "classify": [{"verdict": "answerable", "clarify_question": None, "decline_reason": None}],
            "draft_sql": [
                {"sql": "SELECT COUNT(*) FROM nba.shot_detail WHERE 1=0"},
                {"sql": "SELECT COUNT(*) FROM nba.shot_detail"},
            ],
            "critic": [{"verdict": "ok", "feedback": ""}],
            "summarize": [{"summary": "705 shots."}],
        }
    )
    executor = FakeExecutor(
        outcomes=[
            ExecOutcome(status="ok", result=ExecResult(columns=("n",), rows=())),
            ExecOutcome(status="ok", result=ExecResult(columns=("n",), rows=((705,),))),
        ]
    )
    graph = build_graph(model, executor)

    final = graph.invoke(new_state(QUESTION, user_id=1))

    assert final["outcome"] == "answer"
    assert final["execute_retry_count"] == 1
    assert len(executor.calls) == 2


def test_critic_rejection_retries_draft_sql_then_succeeds():
    """Rung 4: the critic catches valid-but-wrong and sends it back."""
    model = FakeModelClient(
        scripts={
            "classify": [{"verdict": "answerable", "clarify_question": None, "decline_reason": None}],
            "draft_sql": [
                {"sql": "SELECT COUNT(*) FROM nba.shot_detail"},
                {"sql": "SELECT COUNT(*) FROM nba.shot_detail WHERE shot_made_flag = 1"},
            ],
            "critic": [
                {"verdict": "reject", "feedback": "counts attempts, not makes"},
                {"verdict": "ok", "feedback": ""},
            ],
            "summarize": [{"summary": "705 makes."}],
        }
    )
    executor = FakeExecutor(
        outcomes=[
            ExecOutcome(status="ok", result=ExecResult(columns=("n",), rows=((1000,),))),
            ExecOutcome(status="ok", result=ExecResult(columns=("n",), rows=((705,),))),
        ]
    )
    graph = build_graph(model, executor)

    final = graph.invoke(new_state(QUESTION, user_id=1))

    assert final["outcome"] == "answer"
    assert final["critic_retry_count"] == 1
    assert model.calls.count("critic") == 2
    assert model.calls.count("draft_sql") == 2


def test_retry_is_bounded_and_terminates_with_an_error():
    """The plan's hard requirement: retry must never run away. A validator
    that rejects every attempt still terminates, at the configured cap, with
    a reported error rather than looping forever."""
    limits = RetryLimits(execute=2, critic=1, total_model_calls=10)
    # draft_sql is asked for far more replies than the cap should ever consume.
    model = FakeModelClient(
        scripts={
            "classify": [{"verdict": "answerable", "clarify_question": None, "decline_reason": None}],
            "draft_sql": [{"sql": "UPDATE nba.shot_detail SET x = 1"}] * 10,
        }
    )
    # The server rejects every attempt, forever.
    executor = FakeExecutor(
        outcomes=[
            ExecOutcome(status="validation_rejected", error="only SELECT statements are allowed")
        ]
        * 10
    )
    graph = build_graph(model, executor, limits=limits)

    final = graph.invoke(new_state(QUESTION, user_id=1))

    assert final["outcome"] == "answer"  # summarize's contract: never raise, report `error`
    assert final["error"] is not None
    assert final["execute_retry_count"] == limits.execute
    # One draft_sql call per attempt; the cap stops it at exactly `limits.execute`.
    assert model.calls.count("draft_sql") == limits.execute
    assert len(executor.calls) == limits.execute  # one rejected round trip per attempt
    # summarize's error branch reports the failure without spending a model
    # call to narrate an empty result.
    assert "summarize" not in model.calls
    assert final["total_model_calls"] <= limits.total_model_calls
