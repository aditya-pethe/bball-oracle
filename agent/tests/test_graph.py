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


# ---------------------------------------------------------------------------
# Wall-clock budget (.agents/p5_regression_report.md, 2026-08-10)
# ---------------------------------------------------------------------------
#
# RetryLimits bounds MODEL CALLS, but the thing that actually killed a turn in
# production was TIME: /api/agent's `maxDuration = 60`. A ladder that is well
# inside its call budget can still start a rung it cannot finish, and the route
# is torn down mid-node with no answer at all. Six model calls at 10-25s each
# was never going to fit, and nothing in the graph knew that.


def _slow_scripts() -> dict[str, list[dict]]:
    return {
        "classify": [{"verdict": "answerable", "clarify_question": None, "decline_reason": None}],
        "draft_sql": [{"sql": "SELECT 1"}],
        "critic": [{"verdict": "reject", "feedback": "wrong table"}],
        "summarize": [{"summary": "Best effort."}],
    }


def test_a_retry_is_not_started_when_the_wall_clock_budget_is_spent():
    model = FakeModelClient(scripts=_slow_scripts())
    executor = FakeExecutor(
        outcomes=[ExecOutcome(status="ok", result=ExecResult(columns=("n",), rows=((1,),)))]
    )
    graph = build_graph(model, executor, limits=RetryLimits(retry_deadline_ms=0))

    state = dict(new_state(QUESTION, user_id=1))
    final = graph.invoke(state)

    # The critic rejected and the call budget was untouched, so without a clock
    # this would have gone back to draft_sql. It went to summarize instead.
    assert model.calls == ["classify", "draft_sql", "critic", "summarize"]
    assert final["outcome"] == "answer"
    assert final["critic_retry_count"] == 1


def test_a_retry_still_happens_inside_the_budget():
    model = FakeModelClient(
        scripts={
            "classify": [{"verdict": "answerable", "clarify_question": None, "decline_reason": None}],
            "draft_sql": [{"sql": "SELECT 1"}, {"sql": "SELECT 2"}],
            "critic": [{"verdict": "reject", "feedback": "wrong table"}, {"verdict": "ok", "feedback": ""}],
            "summarize": [{"summary": "Fixed."}],
        }
    )
    executor = FakeExecutor(
        outcomes=[
            ExecOutcome(status="ok", result=ExecResult(columns=("n",), rows=((1,),))),
            ExecOutcome(status="ok", result=ExecResult(columns=("n",), rows=((2,),))),
        ]
    )
    graph = build_graph(model, executor, limits=RetryLimits(retry_deadline_ms=60_000))

    final = graph.invoke(dict(new_state(QUESTION, user_id=1)))

    assert model.calls == ["classify", "draft_sql", "critic", "draft_sql", "critic", "summarize"]
    assert final["sql"] == "SELECT 2"


def test_an_execute_retry_also_respects_the_clock():
    model = FakeModelClient(
        scripts={
            "classify": [{"verdict": "answerable", "clarify_question": None, "decline_reason": None}],
            "draft_sql": [{"sql": "SELECT bad"}],
            "summarize": [{"summary": "Could not run it."}],
        }
    )
    executor = FakeExecutor(outcomes=[ExecOutcome(status="timeout", error="statement timeout")])
    graph = build_graph(model, executor, limits=RetryLimits(retry_deadline_ms=0))

    final = graph.invoke(dict(new_state(QUESTION, user_id=1)))

    # One draft, one timed-out execute, then straight out -- no second draft.
    # `summarize` is absent from model.calls on purpose: its retries-exhausted path
    # reports the failure without spending a model call to narrate an empty result.
    assert model.calls == ["classify", "draft_sql"]
    assert executor.calls == ["SELECT bad"]
    assert final["error"] == "statement timeout"
    assert "summarize" in {t.node for t in final["node_timings"]}


def test_a_state_without_a_start_time_is_never_deadline_limited():
    # Callers that hand-build a state dict (older tests, ad-hoc invocations) have
    # no `started_at`. Treating a missing clock as "out of time" would silently
    # disable the correction ladder for them.
    model = FakeModelClient(
        scripts={
            "classify": [{"verdict": "answerable", "clarify_question": None, "decline_reason": None}],
            "draft_sql": [{"sql": "SELECT 1"}, {"sql": "SELECT 2"}],
            "critic": [{"verdict": "reject", "feedback": "no"}, {"verdict": "ok", "feedback": ""}],
            "summarize": [{"summary": "ok"}],
        }
    )
    executor = FakeExecutor(
        outcomes=[
            ExecOutcome(status="ok", result=ExecResult(columns=("n",), rows=((1,),))),
            ExecOutcome(status="ok", result=ExecResult(columns=("n",), rows=((2,),))),
        ]
    )
    graph = build_graph(model, executor, limits=RetryLimits(retry_deadline_ms=0))

    state = dict(new_state(QUESTION, user_id=1))
    state["started_at"] = None

    final = graph.invoke(state)
    assert final["sql"] == "SELECT 2"
