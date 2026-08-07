"""Runner and scoring logic, exercised with fakes.

No database, no API key, no network — the orchestration is deterministic even
though what it orchestrates is not, and that part belongs in CI.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from evals.harness.cases import EvalCase
from evals.harness.envelope import AgentEnvelope, ExecResult
from evals.harness.executors import ExecOutcome
from evals.harness.report import build_run, format_summary, RunMeta
from evals.harness.runner import RunnerConfig, run_case, run_suite
from evals.harness.scoring import CaseResult, score

SEED_SET = Path(__file__).resolve().parents[1] / "text2sql-v0.yaml"


class FakeExecutor:
    """Returns canned outcomes keyed by substring of the SQL."""

    name = "fake"

    def __init__(self, responses: dict[str, ExecOutcome], default: ExecOutcome | None = None):
        self._responses = responses
        self._default = default or ExecOutcome(
            status="ok", result=ExecResult(columns=("n",), rows=((0,),))
        )
        self.calls: list[str] = []

    def execute(self, sql: str) -> ExecOutcome:
        self.calls.append(sql)
        for needle, outcome in self._responses.items():
            if needle in sql:
                return outcome
        return self._default


class FakeAgent:
    name = "fake-agent"

    def __init__(self, envelope: AgentEnvelope):
        self._envelope = envelope
        self.questions: list[str] = []

    def answer(self, question: str) -> AgentEnvelope:
        self.questions.append(question)
        return self._envelope


def answer_case(**overrides) -> EvalCase:
    base = dict(
        id="t1-x", tier=1, question="How many shots?", expects="answer",
        gold_sql="SELECT COUNT(*) FROM nba.shot_detail",
    )
    base.update(overrides)
    return EvalCase(**base)


def ok(rows, columns=("n",)) -> ExecOutcome:
    return ExecOutcome(status="ok", result=ExecResult(columns=columns, rows=tuple(rows)))


class TestRunCase:
    def test_matching_result_scores_correct(self):
        case = answer_case()
        agent = FakeAgent(AgentEnvelope(
            outcome="answer", summary="", sql="SELECT COUNT(*) FROM nba.shot_detail",
            result=ExecResult(columns=("shots",), rows=((218701,),)),
        ))
        result = run_case(case, agent, FakeExecutor({}, default=ok([(218701,)])))
        assert result.execution_correct
        assert result.comparison.matches

    def test_differing_result_scores_incorrect(self):
        case = answer_case()
        agent = FakeAgent(AgentEnvelope(
            outcome="answer", summary="", sql="SELECT 1",
            result=ExecResult(columns=("shots",), rows=((999,),)),
        ))
        result = run_case(case, agent, FakeExecutor({}, default=ok([(218701,)])))
        assert not result.execution_correct
        assert result.comparison.mismatch.kind == "row_values"

    def test_gold_failure_is_reported_separately_from_agent_failure(self):
        # A broken gold query must never look like an agent regression.
        case = answer_case()
        agent = FakeAgent(AgentEnvelope(
            outcome="answer", summary="", sql="SELECT 1",
            result=ExecResult(columns=("n",), rows=((1,),)),
        ))
        broken = FakeExecutor({}, default=ExecOutcome(status="error", error='relation "nba.foo" does not exist'))
        result = run_case(case, agent, broken)
        assert result.gold_error is not None
        assert "does not exist" in result.gold_error
        assert result.comparison is None
        assert not result.execution_correct

    def test_agent_abstention_on_answerable_case_is_not_correct(self):
        case = answer_case()
        agent = FakeAgent(AgentEnvelope(outcome="clarify", summary="Which season?"))
        result = run_case(case, agent, FakeExecutor({}, default=ok([(1,)])))
        assert not result.execution_correct
        assert result.abstained

    def test_judgment_case_skips_gold_execution(self):
        case = EvalCase(
            id="t5-x", tier=5, question="Who won MVP?", expects="decline",
            rubric="No awards data.",
        )
        executor = FakeExecutor({})
        agent = FakeAgent(AgentEnvelope(outcome="decline", summary="No awards data."))
        result = run_case(case, agent, executor)
        assert result.outcome_correct
        assert executor.calls == []  # nothing to execute

    def test_clarify_when_decline_expected_is_wrong(self):
        # Both are abstentions, but they send the user down different paths.
        case = EvalCase(
            id="t5-x", tier=5, question="Who won MVP?", expects="decline", rubric="...",
        )
        agent = FakeAgent(AgentEnvelope(outcome="clarify", summary="Which award?"))
        result = run_case(case, agent, FakeExecutor({}))
        assert result.abstained
        assert not result.outcome_correct


class TestRunSuite:
    def test_only_filter(self):
        agent = FakeAgent(AgentEnvelope(outcome="decline", summary=""))
        results = run_suite(
            agent, SEED_SET,
            gold_executor=FakeExecutor({}),
            config=RunnerConfig(pace_seconds=0),
            only=["t5-awards"],
        )
        assert len(results) == 1
        assert results[0].case.id == "t5-awards"

    def test_unknown_case_id_raises(self):
        agent = FakeAgent(AgentEnvelope(outcome="decline", summary=""))
        with pytest.raises(ValueError, match="no such case id"):
            run_suite(agent, SEED_SET, gold_executor=FakeExecutor({}),
                      config=RunnerConfig(pace_seconds=0), only=["nope"])


class TestScoring:
    def _results(self) -> list[CaseResult]:
        from evals.harness.comparator import Comparison

        answered_right = CaseResult(
            case=answer_case(id="a1"),
            envelope=AgentEnvelope(outcome="answer", summary="", sql="SELECT 1",
                                   result=ExecResult(("n",), ((1,),))),
            comparison=Comparison.ok(),
        )
        answered_wrong = CaseResult(
            case=answer_case(id="a2"),
            envelope=AgentEnvelope(outcome="answer", summary="", sql="SELECT 2",
                                   result=ExecResult(("n",), ((2,),))),
            comparison=Comparison.fail("row_values", "..."),
        )
        ducked = CaseResult(
            case=answer_case(id="a3"),
            envelope=AgentEnvelope(outcome="clarify", summary="which?"),
        )
        declined_right = CaseResult(
            case=EvalCase(id="d1", tier=5, question="q", expects="decline", rubric="r"),
            envelope=AgentEnvelope(outcome="decline", summary=""),
        )
        declined_wrong = CaseResult(
            case=EvalCase(id="d2", tier=5, question="q", expects="decline", rubric="r"),
            envelope=AgentEnvelope(outcome="answer", summary="", sql="SELECT 1",
                                   result=ExecResult(("n",), ((1,),))),
        )
        return [answered_right, answered_wrong, ducked, declined_right, declined_wrong]

    def test_execution_accuracy(self):
        metrics = score(self._results())
        assert metrics.answerable == 3
        assert metrics.execution_correct == 1
        assert metrics.execution_accuracy == pytest.approx(1 / 3)

    def test_false_abstention_rate(self):
        metrics = score(self._results())
        assert metrics.false_abstentions == 1
        assert metrics.false_abstention_rate == pytest.approx(1 / 3)

    def test_abstention_recall_and_precision(self):
        metrics = score(self._results())
        # Two cases should abstain; one did.
        assert metrics.should_abstain == 2
        assert metrics.did_abstain_when_should == 1
        assert metrics.abstention_recall == pytest.approx(0.5)
        # Two abstentions total (one warranted, one on an answerable case).
        assert metrics.did_abstain_total == 2
        assert metrics.abstention_precision == pytest.approx(0.5)

    def test_clarify_everything_is_caught_by_false_abstention(self):
        # The metric's whole reason for existing: perfect recall, terrible product.
        cases = [
            CaseResult(case=answer_case(id=f"a{i}"),
                       envelope=AgentEnvelope(outcome="clarify", summary=""))
            for i in range(3)
        ] + [
            CaseResult(case=EvalCase(id="d1", tier=4, question="q",
                                     expects="clarify", rubric="r"),
                       envelope=AgentEnvelope(outcome="clarify", summary="")),
        ]
        metrics = score(cases)
        assert metrics.abstention_recall == 1.0
        assert metrics.false_abstention_rate == 1.0
        assert metrics.execution_accuracy == 0.0


class TestReport:
    def test_summary_renders_without_a_run(self):
        meta = RunMeta(agent="fake", case_file="text2sql-v0.yaml", model="claude-sonnet-5")
        text = format_summary(TestScoring()._results(), meta)
        assert "execution accuracy" in text
        assert "false-abstention rate" in text
        assert "failures" in text

    def test_run_document_is_json_serialisable(self):
        import json

        meta = RunMeta(agent="fake", case_file="text2sql-v0.yaml")
        run = build_run(TestScoring()._results(), meta)
        encoded = json.dumps(run, default=str)
        assert "execution_accuracy" in encoded
        assert run["metrics"]["answerable"] == 3

    def test_gold_errors_surface_in_summary(self):
        results = [
            CaseResult(
                case=answer_case(id="a1"),
                envelope=AgentEnvelope(outcome="answer", summary="", sql="SELECT 1"),
                gold_error="error: relation does not exist",
            )
        ]
        text = format_summary(results, RunMeta(agent="f", case_file="c"))
        assert "gold SQL errors" in text
        assert "GOLD ERROR" in text
