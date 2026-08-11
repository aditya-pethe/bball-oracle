"""Multi-turn behavior through the real graph, with a scripted model
(.agents/p5_conversational_context.md steps 4 and 5).

No network, no database, no API key. What a fake model cannot prove is that the
real model resolves "what about Lillard?" correctly -- that is what
evals/conversation-v0.yaml is for. What it CAN prove, and what is asserted here,
is everything that has to be true before the model gets a chance: that the right
context reaches the right node, that a pending clarification is folded into one
resumable task, that a previous query is offered as a base to transform, and
that no full result set ever reaches a prompt.
"""
from __future__ import annotations

import json

from agent.context import (
    MAX_CONTEXT_MESSAGES,
    MAX_PREVIEW_ROWS,
    ConversationContext,
    ConversationTurn,
    ResultShape,
)
from agent.execute import ExecOutcome, ExecResult
from agent.graph import build_graph
from agent.state import new_state
from agent.tests.conftest import FakeExecutor, FakeModelClient

CURRY_SQL = (
    "SELECT COUNT(*) AS made_threes FROM nba.shot_detail "
    "WHERE player_name = 'Stephen Curry' AND shot_type = '3PT Field Goal' "
    "AND shot_made_flag = 1"
)


def answered_context(question: str, summary: str, sql: str) -> ConversationContext:
    return ConversationContext(
        turns=[
            ConversationTurn(role="user", text=question),
            ConversationTurn(
                role="assistant",
                text=summary,
                outcome="answer",
                sql=sql,
                result_shape=ResultShape(columns=["made_threes"], row_count=1),
                result_preview=[[357]],
            ),
        ]
    )


def clarify_context(question: str, clarification: str) -> ConversationContext:
    return ConversationContext(
        turns=[
            ConversationTurn(role="user", text=question),
            ConversationTurn(role="assistant", text=clarification, outcome="clarify"),
        ]
    )


def happy_scripts(sql: str, summary: str = "Done.") -> dict[str, list[dict]]:
    return {
        "classify": [{"verdict": "answerable", "clarify_question": None, "decline_reason": None}],
        "draft_sql": [{"sql": sql}],
        "critic": [{"verdict": "ok", "feedback": ""}],
        "summarize": [{"summary": summary}],
    }


def run(question: str, context: ConversationContext | None, *, scripts=None, rows=((357,),)):
    model = FakeModelClient(scripts=scripts or happy_scripts("SELECT 1"))
    executor = FakeExecutor(
        outcomes=[ExecOutcome(status="ok", result=ExecResult(columns=("n",), rows=rows))]
    )
    graph = build_graph(model, executor)
    final = graph.invoke(new_state(question, user_id=1, conversation_context=context))
    return final, model


def prompt_for(model: FakeModelClient, node: str) -> str:
    """Every message the given node was called with, as one searchable string."""
    index = model.calls.index(node)
    return json.dumps(model.messages_log[index])


class TestContextReachesTheRightNodes:
    def test_every_model_calling_node_sees_the_prior_question(self):
        context = answered_context("How many threes did Curry make?", "357.", CURRY_SQL)
        _, model = run("What about Lillard?", context)

        for node in ("classify", "draft_sql", "critic", "summarize"):
            assert "How many threes did Curry make?" in prompt_for(model, node), node

    def test_only_the_drafter_sees_the_previous_sql(self):
        context = answered_context("How many threes did Curry make?", "357.", CURRY_SQL)
        _, model = run("What about Lillard?", context)

        assert CURRY_SQL in prompt_for(model, "draft_sql")
        for node in ("classify", "critic", "summarize"):
            assert CURRY_SQL not in prompt_for(model, node), node

    def test_prior_turns_arrive_as_real_conversation_messages(self):
        context = answered_context("How many threes did Curry make?", "357.", CURRY_SQL)
        _, model = run("What about Lillard?", context)

        messages = model.messages_log[model.calls.index("classify")]
        assert [m["role"] for m in messages] == ["user", "assistant", "user"]
        assert messages[-1]["content"].endswith("What about Lillard?")

    def test_a_first_turn_sends_no_conversation_messages_at_all(self):
        _, model = run("How many threes did Curry make?", None)

        for node in ("classify", "draft_sql", "critic", "summarize"):
            messages = model.messages_log[model.calls.index(node)]
            assert len(messages) == 1, node
            assert messages[0]["role"] == "user"


class TestFollowUpClasses:
    """The four ordinary follow-up classes from the plan's product-behavior table.

    A scripted model cannot demonstrate that the follow-up was *understood*, so
    each of these asserts the drafter was given what understanding it requires:
    the prior question, the prior query to transform, and the shape of what it
    returned.
    """

    def _drafter_prompt(self, follow_up: str) -> str:
        context = answered_context("How many threes did Stephen Curry make?", "357.", CURRY_SQL)
        _, model = run(follow_up, context)
        return prompt_for(model, "draft_sql")

    def test_entity_substitution(self):
        prompt = self._drafter_prompt("What about Damian Lillard?")
        assert "Stephen Curry" in prompt and CURRY_SQL in prompt

    def test_filter_refinement(self):
        prompt = self._drafter_prompt("Only in the fourth quarter.")
        assert CURRY_SQL in prompt

    def test_breakdown(self):
        prompt = self._drafter_prompt("Break that down by period.")
        assert CURRY_SQL in prompt
        assert "made_threes" in prompt  # the result shape it is regrouping

    def test_comparison(self):
        prompt = self._drafter_prompt("Compare him with Jokic.")
        assert CURRY_SQL in prompt

    def test_the_drafter_is_told_a_new_topic_means_a_fresh_query(self):
        # Topic reset is a model judgment, so what is testable offline is that the
        # judgment was actually put to it rather than assumed away.
        prompt = self._drafter_prompt("New question: how many games are loaded?")
        assert "topic" in prompt.lower()

    def test_a_transformation_is_never_a_patch(self):
        prompt = self._drafter_prompt("Only in the fourth quarter.")
        assert "standalone" in prompt.lower() or "complete" in prompt.lower()

    def test_a_failed_previous_turn_is_not_offered_as_a_base(self):
        context = ConversationContext(
            turns=[
                ConversationTurn(role="user", text="q"),
                ConversationTurn(
                    role="assistant",
                    text="(this turn failed: column does not exist)",
                    outcome="answer",
                ),
            ]
        )
        _, model = run("try again but for Lillard", context)
        prompt = prompt_for(model, "draft_sql")
        assert "column does not exist" in prompt
        assert "SQL:" not in prompt


class TestClarificationContinuation:
    def test_the_three_parts_are_folded_into_one_task_for_the_classifier(self):
        context = clarify_context(
            "Who was the most clutch player?",
            "How do you want to define clutch -- last 2 minutes, close margin, or both?",
        )
        _, model = run("last two minutes with the margin within five", context)

        prompt = prompt_for(model, "classify")
        assert "Who was the most clutch player?" in prompt
        assert "How do you want to define clutch" in prompt
        assert "last two minutes with the margin within five" in prompt

    def test_the_classifier_still_runs_rather_than_being_bypassed(self):
        context = clarify_context("Who was most clutch?", "Define clutch?")
        final, model = run("last two minutes", context)

        assert model.calls[0] == "classify"
        assert final["outcome"] == "answer"

    def test_the_classifier_may_still_narrow_or_decline(self):
        context = clarify_context("Who was most clutch?", "Define clutch?")
        scripts = {
            "classify": [
                {
                    "verdict": "ambiguous",
                    "clarify_question": "Within five points, or within three?",
                    "decline_reason": None,
                }
            ]
        }
        final, _ = run("close games", context, scripts=scripts)
        assert final["outcome"] == "clarify"
        assert final["summary"] == "Within five points, or within three?"

    def test_the_drafter_works_on_the_resolved_task_not_the_bare_reply(self):
        context = clarify_context("Who was most clutch?", "Define clutch?")
        _, model = run("last two minutes", context)

        prompt = prompt_for(model, "draft_sql")
        assert "Who was most clutch?" in prompt
        assert "last two minutes" in prompt

    def test_an_answered_thread_leaves_the_question_untouched(self):
        context = answered_context("How many threes did Curry make?", "357.", CURRY_SQL)
        _, model = run("What about Lillard?", context)

        prompt = prompt_for(model, "classify")
        assert "continuation of a clarification" not in prompt


class TestWhatNeverReachesThePrompt:
    def test_no_full_result_set(self):
        rows = [[f"player{i}", i] for i in range(200)]
        context = ConversationContext(
            turns=[
                ConversationTurn(role="user", text="top scorers?"),
                ConversationTurn(
                    role="assistant",
                    text="Here they are.",
                    outcome="answer",
                    sql="SELECT 1",
                    result_shape=ResultShape(columns=["name", "pts"], row_count=200),
                    result_preview=rows,
                ),
            ]
        )
        _, model = run("only the top five", context)
        prompt = prompt_for(model, "draft_sql")

        assert "player0" in prompt
        assert "player199" not in prompt
        assert "200 row(s)" in prompt

    def test_no_timings_or_token_counts(self):
        context = answered_context("q", "a", CURRY_SQL)
        _, model = run("follow up", context)

        for messages in model.messages_log:
            rendered = json.dumps(messages)
            assert "duration_ms" not in rendered
            assert "input_tokens" not in rendered

    def test_the_service_accepts_the_wire_contract_and_uses_it(self, monkeypatch):
        """The one test that crosses the HTTP boundary: the JSON shape
        web/lib/conversation-context.ts produces is what POST /agent accepts."""
        from fastapi.testclient import TestClient

        from agent.service import app, get_executor, get_model_client

        monkeypatch.setenv("AGENT_SERVICE_TOKEN", "wire-contract-token")

        model = FakeModelClient(scripts=happy_scripts("SELECT 1", "Done."))
        executor = FakeExecutor(
            outcomes=[ExecOutcome(status="ok", result=ExecResult(columns=("n",), rows=((1,),)))]
        )
        app.dependency_overrides[get_model_client] = lambda: model
        app.dependency_overrides[get_executor] = lambda: executor
        try:
            client = TestClient(app)
            with client.stream(
                "POST",
                "/agent",
                headers={"Authorization": "Bearer wire-contract-token"},
                json={
                    "question": "What about Lillard?",
                    "user_id": 1,
                    "conversation_id": "7",
                    "conversation_message_id": 42,
                    "context": {
                        "turns": [
                            {"role": "user", "text": "How many threes did Curry make?"},
                            {
                                "role": "assistant",
                                "text": "357.",
                                "outcome": "answer",
                                "sql": CURRY_SQL,
                                "result_shape": {
                                    "columns": ["made_threes"],
                                    "row_count": 1,
                                    "truncated": False,
                                },
                                "result_preview": [[357]],
                            },
                        ]
                    },
                },
            ) as response:
                assert response.status_code == 200
                "".join(response.iter_text())
        finally:
            app.dependency_overrides.clear()

        assert CURRY_SQL in prompt_for(model, "draft_sql")
        assert "How many threes did Curry make?" in prompt_for(model, "classify")

    def test_context_is_re_clamped_on_arrival_not_trusted_as_sent(self):
        # The service must not depend on the producer having applied the bounds:
        # a payload with 40 turns and 100 preview rows is clamped here.
        turns = []
        for i in range(20):
            turns.append(ConversationTurn(role="user", text=f"q{i}"))
            turns.append(
                ConversationTurn(
                    role="assistant",
                    text=f"a{i}",
                    outcome="answer",
                    sql="SELECT 1",
                    result_preview=[[j] for j in range(100)],
                )
            )
        context = ConversationContext(turns=turns)

        assert len(context.turns) <= MAX_CONTEXT_MESSAGES
        assert len(context.turns[-1].result_preview) <= MAX_PREVIEW_ROWS
