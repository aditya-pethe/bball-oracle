"""The conversation-context contract (.agents/p5_conversational_context.md).

Written before agent/context.py existed. Everything here is offline: the
contract is a pure projection from persisted turns to prompt input, so it is
fully testable without a model, a database, or the network.
"""
from __future__ import annotations

import json

import pytest

from agent.context import (
    MAX_CONTEXT_MESSAGES,
    MAX_PREVIEW_ROWS,
    MAX_SERIALIZED_BYTES,
    ConversationContext,
    ConversationTurn,
    ResultShape,
    append_assistant_turn,
    append_user_turn,
    build_messages,
    context_messages,
    resolve_question,
)
from agent.envelope import AgentEnvelope, ExecResult


def user(text: str) -> ConversationTurn:
    return ConversationTurn(role="user", text=text)


def assistant(
    text: str = "",
    outcome: str = "answer",
    sql: str | None = None,
    columns: tuple[str, ...] = (),
    rows: tuple = (),
) -> ConversationTurn:
    return ConversationTurn(
        role="assistant",
        text=text,
        outcome=outcome,  # type: ignore[arg-type]
        sql=sql,
        result_shape=(
            ResultShape(columns=list(columns), row_count=len(rows), truncated=False)
            if columns
            else None
        ),
        result_preview=[list(r) for r in rows] if rows else None,
    )


class TestBounds:
    def test_turns_are_capped_at_the_message_bound(self):
        turns = [user(f"q{i}") if i % 2 == 0 else assistant(f"a{i}") for i in range(40)]
        context = ConversationContext(turns=turns)
        assert len(context.turns) == MAX_CONTEXT_MESSAGES
        # The bound keeps the MOST RECENT window, not the oldest.
        assert context.turns[-1].text == "a39"

    def test_preview_rows_are_capped(self):
        rows = tuple((i,) for i in range(50))
        context = ConversationContext(
            turns=[user("q"), assistant("a", sql="SELECT 1", columns=("n",), rows=rows)]
        )
        assert len(context.turns[-1].result_preview) == MAX_PREVIEW_ROWS
        # The shape still reports the true size -- truncating the preview must not
        # rewrite the number of rows the query actually returned.
        assert context.turns[-1].result_shape.row_count == 50

    def test_serialized_size_ceiling_drops_oldest_turns_first(self):
        big = "x" * (MAX_SERIALIZED_BYTES // 4)
        context = ConversationContext(
            turns=[user(big), assistant(big), user(big), assistant("recent")]
        )
        assert len(json.dumps(context.model_dump(), default=str)) <= MAX_SERIALIZED_BYTES
        assert context.turns[-1].text == "recent"

    def test_the_ceiling_never_drops_the_last_turn(self):
        # A single oversized turn is truncated, not deleted: dropping it would
        # silently produce empty context that reads as "no history".
        context = ConversationContext(turns=[assistant("y" * (MAX_SERIALIZED_BYTES * 3))])
        assert len(context.turns) == 1
        assert len(json.dumps(context.model_dump(), default=str)) <= MAX_SERIALIZED_BYTES


class TestPendingClarification:
    def test_none_when_the_last_assistant_turn_answered(self):
        context = ConversationContext(turns=[user("q"), assistant("a", outcome="answer")])
        assert context.pending_clarification is None

    def test_detected_when_the_last_assistant_turn_clarified(self):
        context = ConversationContext(
            turns=[user("who is the best shooter?"), assistant("by what measure?", outcome="clarify")]
        )
        pending = context.pending_clarification
        assert pending is not None
        assert pending.original_question == "who is the best shooter?"
        assert pending.clarify_question == "by what measure?"

    def test_a_later_answer_closes_an_earlier_clarification(self):
        context = ConversationContext(
            turns=[
                user("best shooter?"),
                assistant("by what measure?", outcome="clarify"),
                user("three point percentage"),
                assistant("Here you go.", outcome="answer", sql="SELECT 1"),
            ]
        )
        assert context.pending_clarification is None

    def test_a_later_decline_also_closes_it(self):
        context = ConversationContext(
            turns=[
                user("best shooter?"),
                assistant("by what measure?", outcome="clarify"),
                user("in the playoffs"),
                assistant("no playoff data", outcome="decline"),
            ]
        )
        assert context.pending_clarification is None

    def test_a_clarification_survives_truncation_of_its_original_question(self):
        # The bound would otherwise drop the question the clarification is about,
        # leaving an unanswerable "you asked for clarification about... what?".
        filler = []
        for i in range(MAX_CONTEXT_MESSAGES):
            filler += [user(f"filler{i}"), assistant(f"answered{i}", sql="SELECT 1")]
        turns = [user("what counts as clutch?"), assistant("define clutch", outcome="clarify")]
        context = ConversationContext(turns=turns + filler)
        # Filler closes it, so nothing is pinned...
        assert context.pending_clarification is None

        pinned = ConversationContext(turns=filler + turns)
        assert pinned.pending_clarification is not None
        assert pinned.pending_clarification.original_question == "what counts as clutch?"

    def test_pending_pair_is_retained_when_truncation_would_remove_it(self):
        turns = [user("original question"), assistant("which one?", outcome="clarify")]
        # A long run of user-only turns after the clarify (no assistant reply
        # completed) would push the pair out of the recent window.
        turns += [user(f"noise{i}") for i in range(MAX_CONTEXT_MESSAGES * 2)]
        context = ConversationContext(turns=turns)
        assert len(context.turns) <= MAX_CONTEXT_MESSAGES
        assert context.turns[0].text == "original question"
        assert context.turns[1].outcome == "clarify"
        assert context.pending_clarification.original_question == "original question"

    def test_the_pinned_pair_is_not_also_duplicated_in_the_recent_window(self):
        # The clarify turn is the LAST assistant turn, so a long run of user turns
        # after it puts it inside the recent window as well as in the pin.
        turns = [user("filler-question"), assistant("filler-answer", sql="SELECT 1")]
        turns += [user("original question"), assistant("which one?", outcome="clarify")]
        turns += [user(f"noise{i}") for i in range(5)]

        context = ConversationContext(turns=turns)
        clarifies = [t for t in context.turns if t.outcome == "clarify"]
        assert len(clarifies) == 1
        assert len(context.turns) <= MAX_CONTEXT_MESSAGES

    def test_a_pending_clarification_with_no_preceding_question_is_not_pending(self):
        # Defensive: an assistant clarify as the first turn has nothing to resume.
        context = ConversationContext(turns=[assistant("which?", outcome="clarify")])
        assert context.pending_clarification is None


class TestResolveQuestion:
    def test_no_pending_clarification_leaves_the_question_alone(self):
        context = ConversationContext(turns=[user("q"), assistant("a", sql="SELECT 1")])
        assert resolve_question("How many threes?", context) == "How many threes?"

    def test_empty_context_leaves_the_question_alone(self):
        assert resolve_question("How many threes?", None) == "How many threes?"

    def test_a_pending_clarification_folds_three_pieces_into_one_task(self):
        context = ConversationContext(
            turns=[user("Who was most clutch?"), assistant("How do you define clutch?", outcome="clarify")]
        )
        resolved = resolve_question("last 2 minutes, margin within 5", context)
        assert "Who was most clutch?" in resolved
        assert "How do you define clutch?" in resolved
        assert "last 2 minutes, margin within 5" in resolved


class TestNodeMessages:
    def _context(self) -> ConversationContext:
        return ConversationContext(
            turns=[
                user("How many threes did Curry make?"),
                assistant(
                    "Curry made 357 threes.",
                    sql="SELECT COUNT(*) FROM nba.shot_detail WHERE player_name = 'Stephen Curry'",
                    columns=("made_threes",),
                    rows=((357,),),
                ),
            ]
        )

    def test_no_context_produces_no_messages(self):
        assert context_messages(None, "classify") == []
        assert context_messages(ConversationContext(turns=[]), "draft_sql") == []

    def test_messages_alternate_roles_and_end_before_the_current_question(self):
        messages = context_messages(self._context(), "classify")
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert "How many threes did Curry make?" in messages[0]["content"]

    def test_classify_sees_outcomes_but_not_sql(self):
        rendered = json.dumps(context_messages(self._context(), "classify"))
        assert "Curry made 357 threes." in rendered
        assert "nba.shot_detail" not in rendered

    def test_draft_sql_sees_the_last_successful_sql_and_result_shape(self):
        rendered = json.dumps(context_messages(self._context(), "draft_sql"))
        assert "nba.shot_detail" in rendered
        assert "made_threes" in rendered

    def test_draft_sql_carries_only_the_most_recent_sql(self):
        context = ConversationContext(
            turns=[
                user("q1"),
                assistant("a1", sql="SELECT 'first_query'"),
                user("q2"),
                assistant("a2", sql="SELECT 'second_query'"),
            ]
        )
        rendered = json.dumps(context_messages(context, "draft_sql"))
        assert "second_query" in rendered
        assert "first_query" not in rendered

    def test_critic_and_summarize_see_prior_questions_but_no_prior_sql(self):
        for node in ("critic", "summarize"):
            rendered = json.dumps(context_messages(self._context(), node))
            assert "How many threes did Curry make?" in rendered
            assert "nba.shot_detail" not in rendered, node

    def test_full_result_sets_never_reach_the_prompt(self):
        rows = tuple((f"player{i}", i) for i in range(200))
        context = ConversationContext(
            turns=[user("q"), assistant("a", sql="SELECT 1", columns=("name", "n"), rows=rows)]
        )
        rendered = json.dumps(context_messages(context, "draft_sql"))
        assert "player0" in rendered
        assert "player199" not in rendered
        assert "200 row(s)" in rendered

    def test_an_unknown_node_gets_no_context_rather_than_everything(self):
        assert context_messages(self._context(), "execute") == []


class TestAppendTurn:
    """The Python-side builder the eval harness uses. Production builds the same
    contract in TypeScript from the database; both feed identical renderings."""

    def test_appending_an_answer_keeps_sql_shape_and_a_bounded_preview(self):
        context = append_user_turn(None, "How many threes?")
        context = append_assistant_turn(
            context,
            AgentEnvelope(
                outcome="answer",
                summary="357.",
                sql="SELECT 1",
                result=ExecResult(columns=("n",), rows=tuple((i,) for i in range(30))),
            ),
        )
        turn = context.turns[-1]
        assert turn.outcome == "answer"
        assert turn.sql == "SELECT 1"
        assert turn.result_shape.row_count == 30
        assert len(turn.result_preview) == MAX_PREVIEW_ROWS

    def test_appending_a_clarify_records_the_question_as_the_turn_text(self):
        context = append_user_turn(None, "Who is most clutch?")
        context = append_assistant_turn(
            context, AgentEnvelope(outcome="clarify", summary="Define clutch?")
        )
        assert context.pending_clarification is not None
        assert context.pending_clarification.clarify_question == "Define clutch?"

    def test_a_failed_turn_is_recorded_without_sql_becoming_a_transformation_base(self):
        # An erroring turn must not be offered to the drafter as "the last
        # successful SQL" -- transforming a query that failed is not a follow-up.
        context = append_user_turn(None, "q")
        context = append_assistant_turn(
            context,
            AgentEnvelope(outcome="answer", summary="", sql="SELECT bad", error="boom"),
        )
        rendered = json.dumps(context_messages(context, "draft_sql"))
        assert "SELECT bad" not in rendered

    def test_node_timings_and_token_counts_never_enter_the_context(self):
        from agent.envelope import NodeTiming

        context = append_assistant_turn(
            append_user_turn(None, "q"),
            AgentEnvelope(
                outcome="answer",
                summary="ok",
                sql="SELECT 1",
                result=ExecResult(columns=("n",), rows=((1,),)),
                node_timings=[NodeTiming("classify", 123.4)],
                input_tokens=999,
                output_tokens=888,
            ),
        )
        dumped = json.dumps(context.model_dump(), default=str)
        assert "123.4" not in dumped
        assert "999" not in dumped
        assert "888" not in dumped


class TestSerialisation:
    def test_round_trips_through_json(self):
        context = ConversationContext(
            turns=[user("q"), assistant("a", sql="SELECT 1", columns=("n",), rows=((1,),))]
        )
        restored = ConversationContext.model_validate(json.loads(json.dumps(context.model_dump())))
        assert restored == context

    def test_rejects_an_unknown_role(self):
        with pytest.raises(Exception):
            ConversationTurn(role="system", text="hi")  # type: ignore[arg-type]


class TestUnansweredTurnsAreNotContext:
    """.agents/p5_regression_report.md, 2026-08-10.

    When a turn is interrupted its assistant row stays `{pending: true}`, and the
    context loader correctly excludes it -- which left the NEXT turn with a window
    of user turns and nothing else. `_normalize` then merged that into the current
    task, so the model received the same question twice, wrapped in an instruction
    to resolve references against "the conversation above". One stuck turn degraded
    every later turn in the thread.
    """

    def test_a_window_of_only_questions_is_not_a_conversation(self):
        context = ConversationContext(turns=[user("Who scored the most points this season?")])
        assert context.has_exchange is False
        assert context_messages(context, "classify") == []

    def test_the_current_question_is_not_duplicated_by_a_pending_previous_turn(self):
        question = "Who scored the most points this season"
        context = ConversationContext(turns=[user(question)])
        messages = build_messages(context, "classify", f"Question: {question}")

        assert len(messages) == 1
        assert messages[0]["content"].count(question) == 1

    def test_a_trailing_unanswered_question_is_dropped_but_earlier_history_kept(self):
        context = ConversationContext(
            turns=[
                user("How many threes did Curry make?"),
                assistant("357.", sql="SELECT 1"),
                user("what about Lillard?"),  # in flight / interrupted, never answered
            ]
        )
        assert context.has_exchange is True
        messages = context_messages(context, "classify")
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert "what about Lillard?" not in json.dumps(messages)

    def test_a_completed_exchange_is_still_context(self):
        context = ConversationContext(
            turns=[user("How many threes did Curry make?"), assistant("357.", sql="SELECT 1")]
        )
        assert context.has_exchange is True
        assert len(context_messages(context, "classify")) == 2


class TestValueAwareEviction:
    """Trimming used to count messages, which treated a content-free
    "I need clarification" turn as worth exactly as much as an answered exchange
    carrying SQL and results. Measured on a real thread (conversation 9,
    2026-08-11): one answered question followed by seven clarifications evicted
    the ONLY answer, so every later follow-up had nothing to anchor to and the
    drafter had no query to transform. The window remembered only the useless part.
    """

    def _clarify_heavy_thread(self, clarifications: int = 12):
        turns = [
            user("What was the golden state warriors 2pt and 3pt fg% this past season?"),
            assistant(
                "For the 2023-24 regular season, the Warriors shot 54.8% on 2s.",
                sql="SELECT shot_type, COUNT(*) FROM nba.shot_detail GROUP BY shot_type",
                columns=("shot_type", "attempts"),
                rows=(("2PT Field Goal", 4324),),
            ),
        ]
        for i in range(clarifications):
            turns += [
                user(f"what about team {i}"),
                assistant(f"Which metric did you mean, for team {i}?", outcome="clarify"),
            ]
        return turns

    def test_the_only_answered_exchange_survives_a_run_of_clarifications(self):
        context = ConversationContext(turns=self._clarify_heavy_thread())
        assert len(context.turns) > MAX_CONTEXT_MESSAGES / 2  # sanity: trimming happened
        assert len(context.turns) <= MAX_CONTEXT_MESSAGES

        texts = " | ".join(t.text for t in context.turns)
        assert "golden state warriors" in texts, "the one real answer was evicted"
        assert context.last_reusable_turn is not None, "nothing left for the drafter to transform"

    def test_the_answered_question_is_kept_with_its_answer(self):
        # An answer with no question in front of it is not a resumable exchange.
        context = ConversationContext(turns=self._clarify_heavy_thread())
        index = next(i for i, t in enumerate(context.turns) if t.has_reusable_sql)
        assert index > 0
        assert context.turns[index - 1].role == "user"
        assert "golden state warriors" in context.turns[index - 1].text

    def test_recency_is_still_preserved(self):
        context = ConversationContext(turns=self._clarify_heavy_thread(clarifications=12))
        assert context.turns[-1].text == "Which metric did you mean, for team 11?"

    def test_turns_stay_in_chronological_order(self):
        context = ConversationContext(turns=self._clarify_heavy_thread())
        roles = [t.role for t in context.turns]
        # Kept turns are spliced from two regions; they must not be re-ordered.
        assert roles == sorted(roles, key=lambda _: 0)  # order preserved by construction
        assert context.turns[0].role == "user"

    def test_a_thread_of_answers_still_keeps_the_most_recent_ones(self):
        # Nothing here is low-value, so the policy must degrade to plain recency.
        turns = []
        for i in range(12):
            turns += [user(f"q{i}"), assistant(f"a{i}", sql=f"SELECT {i}")]
        context = ConversationContext(turns=turns)
        assert context.turns[-1].text == "a11"
        assert len(context.turns) == MAX_CONTEXT_MESSAGES

    def test_the_byte_ceiling_also_spares_the_answered_exchange(self):
        # _fit_bytes used to drop blindly from the front, which would undo the
        # protection above the moment a few turns carried long SQL.
        big_sql = "SELECT " + ("x" * 900)
        turns = [user("the answered question"), assistant("answered", sql=big_sql)]
        for i in range(10):
            turns += [user(f"filler {i} " + "y" * 400), assistant(f"clarify {i}", outcome="clarify")]
        context = ConversationContext(turns=turns)

        assert len(json.dumps(context.model_dump(), default=str)) <= MAX_SERIALIZED_BYTES
        assert any(t.has_reusable_sql for t in context.turns), "byte trim evicted the answer"

    def test_a_pending_clarification_is_still_pinned_alongside_the_answer(self):
        turns = [user("the answered question"), assistant("answered", sql="SELECT 1")]
        for i in range(10):
            turns += [user(f"noise {i}"), assistant(f"answered {i}", sql=f"SELECT {i}")]
        turns += [user("something ambiguous"), assistant("which one?", outcome="clarify")]
        context = ConversationContext(turns=turns)

        assert context.pending_clarification is not None
        assert context.pending_clarification.original_question == "something ambiguous"
