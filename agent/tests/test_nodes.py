"""Per-node unit tests -- each node's build() function exercised directly,
without going through the full graph, so decision logic (not just wiring) is
covered: default/fallback handling, retry-prompt content, and the "no model
call" contract for clarify/decline/summarize's error path.

agent/tests/test_graph.py already covers the end-to-end edges and retry caps
with the real node implementations; these tests cover what each node itself
decides given a scripted model reply.
"""
from __future__ import annotations

from agent.execute import ExecResult
from agent.nodes import classify as classify_node
from agent.nodes import clarify as clarify_node
from agent.nodes import critic as critic_node
from agent.nodes import decline as decline_node
from agent.nodes import draft_sql as draft_sql_node
from agent.nodes import summarize as summarize_node
from agent.state import new_state
from agent.tests.conftest import FakeModelClient


# ---------- classify ----------


def test_classify_answerable_leaves_outcome_unset():
    model = FakeModelClient(
        scripts={"classify": [{"verdict": "answerable", "clarify_question": None, "decline_reason": None}]}
    )
    node = classify_node.build(model)
    update = node(new_state("How many shots were attempted?", user_id=1))

    assert "outcome" not in update
    assert update["total_model_calls"] == 1


def test_classify_ambiguous_sets_clarify_question_verbatim():
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
    node = classify_node.build(model)
    update = node(new_state("Who is the best shooter?", user_id=1))

    assert update["outcome"] == "clarify"
    assert update["clarify_question"] == "Best by efficiency or by volume?"


def test_classify_unanswerable_sets_decline_reason_verbatim():
    model = FakeModelClient(
        scripts={
            "classify": [
                {"verdict": "unanswerable", "clarify_question": None, "decline_reason": "No awards data exists."}
            ]
        }
    )
    node = classify_node.build(model)
    update = node(new_state("Who won MVP?", user_id=1))

    assert update["outcome"] == "decline"
    assert update["decline_reason"] == "No awards data exists."


def test_classify_ambiguous_with_missing_field_falls_back_to_a_default():
    """A malformed reply (schema violated, or the model omitted the required
    field despite the schema) must not crash the node -- it must not raise on
    ordinary failure, per the plan's hard requirements."""
    model = FakeModelClient(
        scripts={"classify": [{"verdict": "ambiguous", "clarify_question": None, "decline_reason": None}]}
    )
    node = classify_node.build(model)
    update = node(new_state("Who is the best shooter?", user_id=1))

    assert update["outcome"] == "clarify"
    assert update["clarify_question"]  # non-empty fallback, not None


def test_classify_does_not_mention_sql_writing_in_its_prompt():
    """classify's job is verdict-only; letting SQL-writing framing leak into
    its user turn would blur the separation the node exists to create."""
    model = FakeModelClient(
        scripts={"classify": [{"verdict": "answerable", "clarify_question": None, "decline_reason": None}]}
    )
    node = classify_node.build(model)
    node(new_state("How many shots were attempted?", user_id=1))

    content = model.messages_log[0][0]["content"]
    assert "Do not draft SQL" in content or "Do not write SQL" in content


# ---------- clarify / decline: no model call ----------


def test_clarify_makes_no_model_call():
    node = clarify_node.build()
    state = new_state("Who is the best shooter?", user_id=1)
    state["clarify_question"] = "Best by what measure?"

    update = node(state)

    assert update == {"outcome": "clarify", "summary": "Best by what measure?"}


def test_clarify_handles_missing_clarify_question():
    node = clarify_node.build()
    update = node(new_state("q", user_id=1))
    assert update["summary"] == ""


def test_decline_makes_no_model_call():
    node = decline_node.build()
    state = new_state("Who won MVP?", user_id=1)
    state["decline_reason"] = "No awards data."

    update = node(state)

    assert update == {"outcome": "decline", "summary": "No awards data."}


# ---------- draft_sql ----------


def test_draft_sql_first_attempt_has_no_retry_language():
    model = FakeModelClient(scripts={"draft_sql": [{"sql": "SELECT COUNT(*) FROM nba.shot_detail"}]})
    node = draft_sql_node.build(model)
    node(new_state("How many shots?", user_id=1))

    content = model.messages_log[0][0]["content"]
    assert "previous" not in content.lower()


def test_draft_sql_retry_includes_prior_sql_and_validation_error():
    model = FakeModelClient(scripts={"draft_sql": [{"sql": "SELECT 1"}]})
    node = draft_sql_node.build(model)
    state = new_state("How many shots?", user_id=1)
    state["sql"] = "UPDATE nba.shot_detail SET shot_made_flag = 1"
    state["validation_error"] = "only SELECT statements are allowed"

    node(state)

    content = model.messages_log[0][0]["content"]
    assert "UPDATE nba.shot_detail SET shot_made_flag = 1" in content
    assert "only SELECT statements are allowed" in content


def test_draft_sql_retry_includes_prior_sql_and_execute_error():
    model = FakeModelClient(scripts={"draft_sql": [{"sql": "SELECT 1"}]})
    node = draft_sql_node.build(model)
    state = new_state("How many shots?", user_id=1)
    state["sql"] = "SELECT nonexistent_column FROM nba.shot_detail"
    state["execute_error"] = 'column "nonexistent_column" does not exist'

    node(state)

    content = model.messages_log[0][0]["content"]
    assert "SELECT nonexistent_column FROM nba.shot_detail" in content
    assert "does not exist" in content


def test_draft_sql_retry_zero_rows_gets_a_distinct_message_from_a_sql_error():
    model = FakeModelClient(scripts={"draft_sql": [{"sql": "SELECT 1"}]})
    node = draft_sql_node.build(model)
    state = new_state("How many shots did X take?", user_id=1)
    state["sql"] = "SELECT COUNT(*) FROM nba.shot_detail WHERE player_name = 'Nobody'"
    state["execute_error"] = "query returned zero rows"

    node(state)

    content = model.messages_log[0][0]["content"]
    assert "zero rows" in content
    assert "filter" in content.lower() or "join" in content.lower()


def test_draft_sql_retry_includes_prior_sql_and_critic_feedback():
    model = FakeModelClient(scripts={"draft_sql": [{"sql": "SELECT 1"}]})
    node = draft_sql_node.build(model)
    state = new_state("How many points did Luka score?", user_id=1)
    state["sql"] = "SELECT SUM(...) FROM nba.shot_detail"
    state["critic_feedback"] = "computes from shot_detail only, missing free throws"

    node(state)

    content = model.messages_log[0][0]["content"]
    assert "SELECT SUM(...) FROM nba.shot_detail" in content
    assert "missing free throws" in content


def test_draft_sql_clears_prior_failure_state_on_the_returned_update():
    model = FakeModelClient(scripts={"draft_sql": [{"sql": "SELECT 1"}]})
    node = draft_sql_node.build(model)
    state = new_state("q", user_id=1)
    state["validation_error"] = "bad"
    state["critic_feedback"] = "bad"

    update = node(state)

    assert update["validation_error"] is None
    assert update["execute_error"] is None
    assert update["critic_verdict"] is None
    assert update["critic_feedback"] is None


def test_draft_sql_blank_sql_reply_becomes_none_not_empty_string():
    model = FakeModelClient(scripts={"draft_sql": [{"sql": "   "}]})
    node = draft_sql_node.build(model)
    update = node(new_state("q", user_id=1))
    assert update["sql"] is None


# ---------- critic ----------


def test_critic_defaults_to_ok_when_verdict_missing():
    model = FakeModelClient(scripts={"critic": [{"feedback": ""}]})
    node = critic_node.build(model)
    state = new_state("q", user_id=1)
    state["sql"] = "SELECT 1"
    state["result"] = ExecResult(columns=("n",), rows=((1,),))

    update = node(state)

    assert update["critic_verdict"] == "ok"
    assert "critic_retry_count" not in update


def test_critic_reject_sets_retry_count():
    model = FakeModelClient(scripts={"critic": [{"verdict": "reject", "feedback": "wrong table"}]})
    node = critic_node.build(model)
    state = new_state("q", user_id=1)
    state["sql"] = "SELECT 1"
    state["result"] = ExecResult(columns=("n",), rows=((1,),))

    update = node(state)

    assert update["critic_verdict"] == "reject"
    assert update["critic_retry_count"] == 1
    assert update["critic_feedback"] == "wrong table"


def test_critic_prompt_includes_a_row_sample_not_the_full_result():
    model = FakeModelClient(scripts={"critic": [{"verdict": "ok", "feedback": ""}]})
    node = critic_node.build(model)
    state = new_state("q", user_id=1)
    state["sql"] = "SELECT * FROM nba.shot_detail"
    state["result"] = ExecResult(
        columns=("player_name", "points"),
        rows=tuple((f"Player {i}", i) for i in range(50)),
    )

    node(state)

    content = model.messages_log[0][0]["content"]
    assert "Player 0" in content
    assert "50 row" in content
    # Only a handful of rows should appear verbatim, not all 50.
    assert "Player 10" not in content


# ---------- summarize ----------


def test_summarize_exhausted_retry_skips_the_model_call():
    model = FakeModelClient(scripts={})
    node = summarize_node.build(model)
    state = new_state("q", user_id=1)
    state["execute_error"] = "statement timeout"
    state["result"] = None

    update = node(state)

    assert update == {"outcome": "answer", "error": "statement timeout"}
    assert model.calls == []


def test_summarize_narrates_a_surviving_zero_row_result_instead_of_erroring():
    """Rung 3 exhausted but the (empty) result object survived -- this is a
    legitimate, if unhelpful, answer worth narrating, not a system failure."""
    model = FakeModelClient(scripts={"summarize": [{"summary": "No players met the threshold."}]})
    node = summarize_node.build(model)
    state = new_state("q", user_id=1)
    state["execute_error"] = "query returned zero rows"
    state["result"] = ExecResult(columns=("player_name",), rows=())

    update = node(state)

    assert update["outcome"] == "answer"
    assert "error" not in update
    assert model.calls == ["summarize"]


def test_summarize_prompt_includes_actual_result_rows():
    model = FakeModelClient(scripts={"summarize": [{"summary": "Luka led with 2370 points."}]})
    node = summarize_node.build(model)
    state = new_state("Who scored the most points?", user_id=1)
    state["sql"] = "SELECT player_name, points FROM leaders"
    state["result"] = ExecResult(columns=("player_name", "points"), rows=(("Luka Doncic", 2370),))

    node(state)

    content = model.messages_log[0][0]["content"]
    assert "Luka Doncic" in content
    assert "2370" in content


def test_summarize_truncates_large_result_sets_in_the_prompt():
    model = FakeModelClient(scripts={"summarize": [{"summary": "..."}]})
    node = summarize_node.build(model)
    state = new_state("q", user_id=1)
    state["sql"] = "SELECT * FROM nba.shot_detail"
    state["result"] = ExecResult(
        columns=("n",),
        rows=tuple((i,) for i in range(500)),
    )

    node(state)

    content = model.messages_log[0][0]["content"]
    assert "more row" in content


class TestCallerErrorsAreNotRetried:
    """A caller error must not be mistaken for a validator rejection.

    Regression test for a bug found on the first end-to-end run: an unknown
    `userId` came back as a 400 with no `durationMs`, which looked exactly like
    a validator rejection, so the graph redrafted the SQL twice against a
    failure no SQL change could fix -- burning the retry budget and two model
    calls before giving up.
    """

    def test_unknown_user_is_fatal_not_a_rung(self):
        from agent.execute import ExecOutcome
        from agent.nodes import execute as execute_node
        from agent.tests.conftest import FakeExecutor

        executor = FakeExecutor(
            outcomes=[ExecOutcome(status="config_error", error="unknown userId 1")]
        )
        node = execute_node.build(executor)
        update = node({"sql": "SELECT 1", "user_id": 1})

        assert update["fatal_error"] == "unknown userId 1"
        # Crucially: no retry counter was incremented.
        assert "execute_retry_count" not in update
        assert update["validation_error"] is None

    def test_graph_stops_immediately_on_a_caller_error(self):
        from agent.execute import ExecOutcome
        from agent.graph import build_graph
        from agent.state import new_state
        from agent.tests.conftest import FakeExecutor, FakeModelClient

        model = FakeModelClient(
            scripts={
                "classify": [
                    {"verdict": "answerable", "clarify_question": None, "decline_reason": None}
                ],
                # Only ONE draft is scripted. If the graph retries, FakeModelClient
                # raises -- which is the assertion.
                "draft_sql": [{"sql": "SELECT COUNT(*) FROM nba.shot_detail"}],
            }
        )
        executor = FakeExecutor(
            outcomes=[ExecOutcome(status="config_error", error="unknown userId 99")]
        )
        final = build_graph(model, executor).invoke(new_state("How many shots?", user_id=99))

        assert final["error"] is not None
        assert "unknown userId 99" in final["error"]
        assert model.calls.count("draft_sql") == 1  # no wasted redraft
        assert len(executor.calls) == 1
