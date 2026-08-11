"""The multi-turn eval harness: case loading, running, and scoring.

Offline and deterministic, like evals/tests/test_runner.py — the orchestration is
what is being tested, not the model. The one test that drives the real graph does
it with a scripted fake model and a scripted executor, which is what makes
"context survived from turn 1 to turn 2" provable without spending anything.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from agent.envelope import AgentEnvelope, ExecResult
from evals.harness.conversation_cases import (
    ConversationCase,
    ConversationCaseError,
    load_conversation_cases,
    parse_conversation_case,
)
from evals.harness.conversation_report import (
    build_conversation_run,
    format_conversation_summary,
)
from evals.harness.conversation_runner import (
    ConversationResult,
    StatelessConversationAgent,
    run_conversation_suite,
)
from evals.harness.conversation_scoring import score_conversations
from evals.harness.executors import ExecOutcome
from evals.harness.report import RunMeta
from evals.harness.scoring import CaseResult

SEED_SET = Path(__file__).resolve().parents[1] / "conversation-v0.yaml"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeExecutor:
    name = "fake"

    def __init__(self, rows_by_needle: dict[str, tuple] | None = None, default=((1,),)):
        self._rows = rows_by_needle or {}
        self._default = default
        self.calls: list[str] = []

    def execute(self, sql: str) -> ExecOutcome:
        self.calls.append(sql)
        for needle, rows in self._rows.items():
            if needle in sql:
                return ExecOutcome(status="ok", result=ExecResult(("n",), tuple(rows)))
        return ExecOutcome(status="ok", result=ExecResult(("n",), tuple(self._default)))


class ScriptedConversationAgent:
    """Replies from a fixed script, and records which session each question
    arrived on so "context was carried" is observable."""

    name = "scripted"

    def __init__(self, envelopes: list[AgentEnvelope]):
        self._envelopes = list(envelopes)
        self.sessions: list[list[str]] = []

    def new_conversation(self):
        asked: list[str] = []
        self.sessions.append(asked)
        agent = self

        class _Session:
            def ask(self, question: str) -> AgentEnvelope:
                asked.append(question)
                return agent._envelopes.pop(0)

        return _Session()


def answered(rows=((1,),), sql="SELECT 1", summary="ok") -> AgentEnvelope:
    return AgentEnvelope(
        outcome="answer", summary=summary, sql=sql, result=ExecResult(("n",), tuple(rows))
    )


def clarified(text="Which one?") -> AgentEnvelope:
    return AgentEnvelope(outcome="clarify", summary=text)


def declined(text="No such data.") -> AgentEnvelope:
    return AgentEnvelope(outcome="decline", summary=text)


def case_dict(**overrides) -> dict:
    base = {
        "id": "c1",
        "tier": 2,
        "turns": [
            {"user": "How many threes did Curry make?", "expects": "answer", "gold_sql": "SELECT 1"},
            {"user": "What about Lillard?", "expects": "answer", "gold_sql": "SELECT 2"},
        ],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


class TestLoading:
    def test_parses_ordered_turns(self):
        case = parse_conversation_case(case_dict())
        assert isinstance(case, ConversationCase)
        assert [t.index for t in case.turns] == [0, 1]
        assert [t.question for t in case.turns] == [
            "How many threes did Curry make?",
            "What about Lillard?",
        ]

    def test_turn_ids_are_unique_and_traceable_to_the_conversation(self):
        case = parse_conversation_case(case_dict())
        assert [t.id for t in case.turns] == ["c1#1", "c1#2"]

    def test_a_conversation_needs_at_least_two_turns(self):
        # A one-turn "conversation" is a single-turn case; it belongs in
        # text2sql-v0.yaml where it stays comparable with the Phase 4 baseline.
        with pytest.raises(ConversationCaseError, match="at least two turns"):
            parse_conversation_case(
                case_dict(turns=[{"user": "q", "expects": "answer", "gold_sql": "SELECT 1"}])
            )

    def test_an_answer_turn_requires_gold_sql(self):
        with pytest.raises(ConversationCaseError, match="gold_sql"):
            parse_conversation_case(
                case_dict(turns=[{"user": "a", "expects": "answer", "gold_sql": "SELECT 1"},
                                 {"user": "b", "expects": "answer"}])
            )

    def test_a_clarify_turn_requires_a_rubric_and_forbids_gold_sql(self):
        with pytest.raises(ConversationCaseError, match="rubric"):
            parse_conversation_case(
                case_dict(turns=[{"user": "a", "expects": "clarify"},
                                 {"user": "b", "expects": "answer", "gold_sql": "SELECT 1"}])
            )
        with pytest.raises(ConversationCaseError, match="must not carry gold_sql"):
            parse_conversation_case(
                case_dict(turns=[{"user": "a", "expects": "clarify", "rubric": "r",
                                  "gold_sql": "SELECT 1"},
                                 {"user": "b", "expects": "answer", "gold_sql": "SELECT 1"}])
            )

    def test_unknown_keys_are_rejected_at_load_time(self):
        with pytest.raises(ConversationCaseError, match="unknown keys"):
            parse_conversation_case(
                case_dict(turns=[{"user": "a", "expects": "answer", "gold_sql": "SELECT 1",
                                  "expcts": "typo"},
                                 {"user": "b", "expects": "answer", "gold_sql": "SELECT 1"}])
            )

    def test_a_turn_following_a_clarify_is_marked_as_a_continuation(self):
        case = parse_conversation_case(
            case_dict(turns=[
                {"user": "who was most clutch?", "expects": "clarify", "rubric": "asks for a definition"},
                {"user": "last two minutes", "expects": "answer", "gold_sql": "SELECT 1"},
            ])
        )
        assert case.turns[0].follows_clarify is False
        assert case.turns[1].follows_clarify is True

    def test_reset_turns_are_flagged(self):
        case = parse_conversation_case(
            case_dict(turns=[
                {"user": "curry's threes?", "expects": "answer", "gold_sql": "SELECT 1"},
                {"user": "New question: how many games are loaded?", "expects": "answer",
                 "gold_sql": "SELECT 2", "reset": True},
            ])
        )
        assert [t.reset for t in case.turns] == [False, True]

    def test_reset_is_not_allowed_on_a_first_turn(self):
        # Nothing to reset away from; the flag would score a metric that has no
        # meaning and quietly inflate context-reset accuracy.
        with pytest.raises(ConversationCaseError, match="first turn"):
            parse_conversation_case(
                case_dict(turns=[
                    {"user": "a", "expects": "answer", "gold_sql": "SELECT 1", "reset": True},
                    {"user": "b", "expects": "answer", "gold_sql": "SELECT 2"},
                ])
            )

    def test_duplicate_case_ids_are_rejected(self, tmp_path):
        path = tmp_path / "dupes.yaml"
        path.write_text(yaml.safe_dump({"cases": [case_dict(), case_dict()]}))
        with pytest.raises(ConversationCaseError, match="duplicate"):
            load_conversation_cases(path)

    def test_the_seed_set_loads_and_covers_the_required_follow_up_classes(self):
        cases = load_conversation_cases(SEED_SET)
        assert len(cases) >= 10

        every_turn = [t for c in cases for t in c.turns]
        assert any(t.follows_clarify for t in every_turn), "no clarification continuation"
        assert any(t.reset for t in every_turn), "no topic reset"
        assert any(len(c.turns) >= 3 for c in cases), "no three-or-more-turn chain"
        assert any(t.expects == "decline" for t in every_turn), "no decline"

        # Every answer turn must be executable; every judgment turn must be judgeable.
        for turn in every_turn:
            if turn.expects == "answer":
                assert turn.gold_sql, turn.id
            else:
                assert turn.rubric, turn.id


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


class TestRunning:
    def _suite(self, tmp_path: Path, cases: list[dict]) -> Path:
        path = tmp_path / "cases.yaml"
        path.write_text(yaml.safe_dump({"cases": cases}))
        return path

    def test_each_conversation_gets_its_own_session(self, tmp_path):
        agent = ScriptedConversationAgent([answered(), answered(), answered(), answered()])
        path = self._suite(tmp_path, [case_dict(id="c1"), case_dict(id="c2")])

        run_conversation_suite(agent, path, gold_executor=FakeExecutor(), pace_seconds=0)

        assert len(agent.sessions) == 2
        assert all(len(session) == 2 for session in agent.sessions)

    def test_turns_are_asked_in_order(self, tmp_path):
        agent = ScriptedConversationAgent([answered(), answered()])
        path = self._suite(tmp_path, [case_dict()])

        run_conversation_suite(agent, path, gold_executor=FakeExecutor(), pace_seconds=0)

        assert agent.sessions[0] == [
            "How many threes did Curry make?",
            "What about Lillard?",
        ]

    def test_gold_sql_runs_only_for_answer_turns(self, tmp_path):
        agent = ScriptedConversationAgent([clarified(), answered()])
        path = self._suite(tmp_path, [case_dict(turns=[
            {"user": "who is best?", "expects": "clarify", "rubric": "asks by what measure"},
            {"user": "three point percentage", "expects": "answer", "gold_sql": "SELECT gold"},
        ])])
        executor = FakeExecutor()

        run_conversation_suite(agent, path, gold_executor=executor, pace_seconds=0)

        assert executor.calls == ["SELECT gold"]

    def test_a_turn_is_scored_by_execution_against_its_own_gold(self, tmp_path):
        agent = ScriptedConversationAgent([answered(rows=((1,),)), answered(rows=((999,),))])
        path = self._suite(tmp_path, [case_dict(turns=[
            {"user": "a", "expects": "answer", "gold_sql": "SELECT one"},
            {"user": "b", "expects": "answer", "gold_sql": "SELECT two"},
        ])])
        executor = FakeExecutor({"one": ((1,),), "two": ((2,),)})

        [result] = run_conversation_suite(agent, path, gold_executor=executor, pace_seconds=0)

        assert [t.execution_correct for t in result.turns] == [True, False]
        assert result.passed is False

    def test_a_later_turn_still_runs_after_an_earlier_one_fails(self, tmp_path):
        # A conversation is not abandoned at the first miss: the follow-up's own
        # accuracy is exactly what this suite exists to measure.
        agent = ScriptedConversationAgent([answered(rows=((999,),)), answered(rows=((1,),))])
        path = self._suite(tmp_path, [case_dict()])

        [result] = run_conversation_suite(agent, path, gold_executor=FakeExecutor(), pace_seconds=0)

        assert len(result.turns) == 2
        assert [t.execution_correct for t in result.turns] == [False, True]

    def test_a_broken_gold_query_is_reported_as_a_case_bug_not_an_agent_miss(self, tmp_path):
        class Broken(FakeExecutor):
            def execute(self, sql: str) -> ExecOutcome:
                self.calls.append(sql)
                return ExecOutcome(status="error", error='relation "nba.foo" does not exist')

        agent = ScriptedConversationAgent([answered(), answered()])
        path = self._suite(tmp_path, [case_dict()])

        [result] = run_conversation_suite(agent, path, gold_executor=Broken(), pace_seconds=0)

        assert all(t.gold_error for t in result.turns)

    def test_only_filters_by_conversation_id(self, tmp_path):
        agent = ScriptedConversationAgent([answered(), answered()])
        path = self._suite(tmp_path, [case_dict(id="c1"), case_dict(id="c2")])

        results = run_conversation_suite(
            agent, path, gold_executor=FakeExecutor(), pace_seconds=0, only=["c2"]
        )
        assert [r.case.id for r in results] == ["c2"]

    def test_the_stateless_adapter_runs_a_single_turn_agent_as_a_control(self, tmp_path):
        class SingleTurn:
            name = "single"

            def __init__(self):
                self.questions: list[str] = []

            def answer(self, question: str) -> AgentEnvelope:
                self.questions.append(question)
                return answered()

        inner = SingleTurn()
        agent = StatelessConversationAgent(inner)
        path = self._suite(tmp_path, [case_dict()])

        run_conversation_suite(agent, path, gold_executor=FakeExecutor(), pace_seconds=0)

        assert inner.questions == ["How many threes did Curry make?", "What about Lillard?"]
        assert "no-context" in agent.name


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def turn_result(case_turn, envelope, *, correct: bool) -> CaseResult:
    from evals.harness.comparator import Comparison

    comparison = None
    if case_turn.is_execution_scored and envelope.outcome == "answer":
        comparison = Comparison.ok() if correct else Comparison.fail("row_values", "...")
    return CaseResult(case=case_turn, envelope=envelope, comparison=comparison)


class TestScoring:
    def _case(self, **overrides) -> ConversationCase:
        return parse_conversation_case(case_dict(**overrides))

    def test_turn_and_followup_accuracy_are_reported_separately(self):
        case = self._case()
        results = [
            ConversationResult(
                case=case,
                turns=[
                    turn_result(case.turns[0], answered(), correct=True),
                    turn_result(case.turns[1], answered(), correct=False),
                ],
            )
        ]
        metrics = score_conversations(results)

        assert metrics.turns.execution_accuracy == pytest.approx(0.5)
        # The whole point of the suite: the first turn was fine, the follow-up was not.
        assert metrics.followups.execution_accuracy == 0.0
        assert metrics.first_turns.execution_accuracy == 1.0

    def test_conversation_success_requires_every_scored_turn_to_pass(self):
        case = self._case()
        good = ConversationResult(case=case, turns=[
            turn_result(case.turns[0], answered(), correct=True),
            turn_result(case.turns[1], answered(), correct=True),
        ])
        bad = ConversationResult(case=case, turns=[
            turn_result(case.turns[0], answered(), correct=True),
            turn_result(case.turns[1], answered(), correct=False),
        ])
        metrics = score_conversations([good, bad])

        assert metrics.conversations == 2
        assert metrics.conversations_passed == 1
        assert metrics.conversation_success_rate == pytest.approx(0.5)

    def test_outcome_accuracy_counts_the_right_kind_of_response_per_turn(self):
        case = self._case(turns=[
            {"user": "a", "expects": "clarify", "rubric": "r"},
            {"user": "b", "expects": "answer", "gold_sql": "SELECT 1"},
        ])
        results = [ConversationResult(case=case, turns=[
            turn_result(case.turns[0], clarified(), correct=False),
            turn_result(case.turns[1], declined(), correct=False),
        ])]
        metrics = score_conversations(results)
        assert metrics.turns.outcome_accuracy == pytest.approx(0.5)

    def test_clarification_continuation_counts_only_turns_after_a_clarify(self):
        case = self._case(turns=[
            {"user": "who is best?", "expects": "clarify", "rubric": "r"},
            {"user": "by three point percentage", "expects": "answer", "gold_sql": "SELECT 1"},
        ])
        continued = ConversationResult(case=case, turns=[
            turn_result(case.turns[0], clarified(), correct=False),
            turn_result(case.turns[1], answered(), correct=True),
        ])
        gave_up = ConversationResult(case=case, turns=[
            turn_result(case.turns[0], clarified(), correct=False),
            turn_result(case.turns[1], declined(), correct=False),
        ])
        metrics = score_conversations([continued, gave_up])

        assert metrics.clarification_continuations == 2
        assert metrics.clarification_continued == 1
        assert metrics.clarification_continuation_rate == pytest.approx(0.5)

    def test_a_turn_the_agent_never_clarified_is_not_a_continuation(self):
        # The case expects a clarification at turn 1 and the agent answered instead,
        # so continuation was never exercised. Counting it would blame this metric
        # for a miss outcome accuracy already records.
        case = self._case(turns=[
            {"user": "who is best?", "expects": "clarify", "rubric": "r"},
            {"user": "by three point percentage", "expects": "answer", "gold_sql": "SELECT 1"},
        ])
        results = [ConversationResult(case=case, turns=[
            turn_result(case.turns[0], answered(), correct=False),
            turn_result(case.turns[1], answered(), correct=True),
        ])]
        metrics = score_conversations(results)

        assert metrics.clarification_continuations == 0
        # ...and the turn-1 miss is still visible where it belongs.
        assert metrics.turns.outcome_correct == 1

    def test_a_narrower_clarification_still_counts_as_a_continuation(self):
        case = self._case(turns=[
            {"user": "who is best?", "expects": "clarify", "rubric": "r"},
            {"user": "shooting", "expects": "answer", "gold_sql": "SELECT 1"},
        ])
        results = [ConversationResult(case=case, turns=[
            turn_result(case.turns[0], clarified(), correct=False),
            turn_result(case.turns[1], clarified("Field goals or threes?"), correct=False),
        ])]
        metrics = score_conversations(results)
        assert metrics.clarification_continued == 1
        # ...but it is still an execution miss: it did not answer.
        assert metrics.turns.execution_correct == 0

    def test_context_reset_accuracy_covers_only_flagged_turns(self):
        case = self._case(turns=[
            {"user": "curry's threes?", "expects": "answer", "gold_sql": "SELECT 1"},
            {"user": "New question: how many games?", "expects": "answer",
             "gold_sql": "SELECT 2", "reset": True},
        ])
        hit = ConversationResult(case=case, turns=[
            turn_result(case.turns[0], answered(), correct=True),
            turn_result(case.turns[1], answered(), correct=True),
        ])
        miss = ConversationResult(case=case, turns=[
            turn_result(case.turns[0], answered(), correct=True),
            turn_result(case.turns[1], answered(), correct=False),
        ])
        metrics = score_conversations([hit, miss])

        assert metrics.reset_turns == 2
        assert metrics.reset_correct == 1
        assert metrics.context_reset_accuracy == pytest.approx(0.5)

    def test_latency_and_tokens_are_broken_out_by_turn_index(self):
        case = self._case()
        first = answered()
        first.total_ms, first.input_tokens = 1000.0, 100
        second = answered()
        second.total_ms, second.input_tokens = 3000.0, 400

        metrics = score_conversations([ConversationResult(case=case, turns=[
            turn_result(case.turns[0], first, correct=True),
            turn_result(case.turns[1], second, correct=True),
        ])])

        assert metrics.by_turn_index[0].latency_p50 == 1000.0
        assert metrics.by_turn_index[1].latency_p50 == 3000.0
        # The cost of carrying context, which is the number the bounds get tuned on.
        assert metrics.by_turn_index[1].input_tokens > metrics.by_turn_index[0].input_tokens


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


class TestReport:
    def _results(self) -> list[ConversationResult]:
        case = parse_conversation_case(case_dict())
        return [ConversationResult(case=case, turns=[
            turn_result(case.turns[0], answered(), correct=True),
            turn_result(case.turns[1], answered(), correct=False),
        ])]

    def test_summary_reports_follow_up_and_whole_conversation_separately(self):
        text = format_conversation_summary(
            self._results(), RunMeta(agent="fake", case_file="conversation-v0.yaml")
        )
        assert "follow-up execution accuracy" in text
        assert "conversation success rate" in text
        assert "clarification continuation" in text

    def test_run_document_is_json_serialisable_and_keeps_per_turn_detail(self):
        run = build_conversation_run(
            self._results(), RunMeta(agent="fake", case_file="conversation-v0.yaml")
        )
        encoded = json.dumps(run, default=str)
        assert "followup_execution_accuracy" in encoded
        assert run["conversations"][0]["turns"][1]["execution_correct"] is False
        assert run["metrics"]["conversation_success_rate"] == 0.0


# ---------------------------------------------------------------------------
# The real graph, driven with a scripted model
# ---------------------------------------------------------------------------


class TestConversationGraphAgent:
    """The eval adapter must carry context between turns using the SAME contract
    production uses (agent/context.py), not an eval-only prompt path."""

    def _agent(self, scripts, outcomes):
        from agent.tests.conftest import FakeExecutor as GraphExecutor, FakeModelClient
        from evals.harness.graph_agent import ConversationGraphAgent

        model = FakeModelClient(scripts=scripts)
        agent = ConversationGraphAgent(
            model_client=model, executor=GraphExecutor(outcomes=outcomes), user_id=1
        )
        return agent, model

    def test_the_second_turn_sees_the_first_turns_question_and_sql(self):
        from agent.execute import ExecOutcome as GraphOutcome

        scripts = {
            "classify": [{"verdict": "answerable", "clarify_question": None, "decline_reason": None}] * 2,
            "draft_sql": [{"sql": "SELECT curry"}, {"sql": "SELECT lillard"}],
            "critic": [{"verdict": "ok", "feedback": ""}] * 2,
            "summarize": [{"summary": "357."}, {"summary": "212."}],
        }
        outcomes = [
            GraphOutcome(status="ok", result=ExecResult(("n",), ((357,),))),
            GraphOutcome(status="ok", result=ExecResult(("n",), ((212,),))),
        ]
        agent, model = self._agent(scripts, outcomes)

        session = agent.new_conversation()
        session.ask("How many threes did Curry make?")
        session.ask("What about Lillard?")

        second_draft = json.dumps(model.messages_log[model.calls.index("draft_sql", 2)])
        assert "How many threes did Curry make?" in second_draft
        assert "SELECT curry" in second_draft

    def test_a_new_conversation_starts_from_no_context(self):
        from agent.execute import ExecOutcome as GraphOutcome

        scripts = {
            "classify": [{"verdict": "answerable", "clarify_question": None, "decline_reason": None}] * 2,
            "draft_sql": [{"sql": "SELECT first"}, {"sql": "SELECT second"}],
            "critic": [{"verdict": "ok", "feedback": ""}] * 2,
            "summarize": [{"summary": "a"}, {"summary": "b"}],
        }
        outcomes = [GraphOutcome(status="ok", result=ExecResult(("n",), ((1,),)))] * 2
        agent, model = self._agent(scripts, outcomes)

        agent.new_conversation().ask("first thread question")
        agent.new_conversation().ask("second thread question")

        second_classify = json.dumps(model.messages_log[model.calls.index("classify", 4)])
        assert "first thread question" not in second_classify

    def test_a_clarification_is_resumed_on_the_next_turn(self):
        from agent.execute import ExecOutcome as GraphOutcome

        scripts = {
            "classify": [
                {"verdict": "ambiguous", "clarify_question": "How do you define clutch?",
                 "decline_reason": None},
                {"verdict": "answerable", "clarify_question": None, "decline_reason": None},
            ],
            "draft_sql": [{"sql": "SELECT clutch"}],
            "critic": [{"verdict": "ok", "feedback": ""}],
            "summarize": [{"summary": "Jokic."}],
        }
        outcomes = [GraphOutcome(status="ok", result=ExecResult(("n",), (("Jokic",),)))]
        agent, model = self._agent(scripts, outcomes)

        session = agent.new_conversation()
        first = session.ask("Who was the most clutch player?")
        second = session.ask("last two minutes, margin within five")

        assert first.outcome == "clarify"
        assert second.outcome == "answer"

        resumed = json.dumps(model.messages_log[model.calls.index("classify", 1)])
        assert "Who was the most clutch player?" in resumed
        assert "How do you define clutch?" in resumed
        assert "last two minutes, margin within five" in resumed
