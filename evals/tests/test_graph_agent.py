"""GraphAgent, exercised with fakes -- no network, no API key, no LangSmith.

Mirrors agent/tests/conftest.py's FakeModelClient/FakeExecutor pattern (same
scripted-reply convention) but stays self-contained here rather than importing
agent's test fixtures: evals/ owns its own test doubles for the thing it is
measuring, the same way evals/harness/executors.py's FakeExecutor-equivalents
in evals/tests/test_runner.py do not reach into agent/.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agent.execute import ExecOutcome
from agent.execute import ExecResult as AgentExecResult
from agent.llm import ModelReply
from evals.harness.graph_agent import GraphAgent

QUESTION = "How many made shots did Luka Doncic have?"


@dataclass
class FakeModelClient:
    scripts: dict[str, list[dict]]
    calls: list[str] = field(default_factory=list)
    conversation_message_ids: list[int | None] = field(default_factory=list)
    fail_on: str | None = None

    def complete(self, node: str, *, messages: list[dict], schema: dict) -> ModelReply:
        if node == self.fail_on:
            raise RuntimeError("simulated API failure")
        self.calls.append(node)
        queue = self.scripts.get(node)
        if not queue:
            raise AssertionError(f"no more scripted replies for node {node!r}")
        payload = queue.pop(0)
        return ModelReply(
            payload=payload, input_tokens=10, output_tokens=5,
            cache_creation_input_tokens=3, cache_read_input_tokens=7,
        )


@dataclass
class FakeExecutor:
    outcomes: list[ExecOutcome]
    calls: list[tuple[str, int]] = field(default_factory=list)
    conversation_message_ids: list[int | None] = field(default_factory=list)

    def execute(
        self,
        sql: str,
        user_id: int,
        conversation_message_id: int | None = None,
    ) -> ExecOutcome:
        self.conversation_message_ids.append(conversation_message_id)
        self.calls.append((sql, user_id))
        if not self.outcomes:
            raise AssertionError("no more scripted executor outcomes")
        return self.outcomes.pop(0)


def _happy_scripts() -> dict[str, list[dict]]:
    return {
        "classify": [{"verdict": "answerable", "clarify_question": None, "decline_reason": None}],
        "draft_sql": [{"sql": "SELECT COUNT(*) FROM nba.shot_detail"}],
        "critic": [{"verdict": "ok", "feedback": ""}],
        "summarize": [{"summary": "Luka made 705 shots."}],
    }


class TestName:
    def test_name_includes_model(self):
        model = FakeModelClient(scripts=_happy_scripts())
        executor = FakeExecutor(outcomes=[ExecOutcome(status="ok", result=AgentExecResult(("n",), ((705,),)))])
        agent = GraphAgent(model_client=model, executor=executor, user_id=1, model="claude-sonnet-5")
        assert agent.name == "graph-claude-sonnet-5"


class TestHappyPath:
    def test_maps_final_state_onto_envelope(self):
        model = FakeModelClient(scripts=_happy_scripts())
        executor = FakeExecutor(
            outcomes=[ExecOutcome(status="ok", result=AgentExecResult(("n",), ((705,),)))]
        )
        agent = GraphAgent(model_client=model, executor=executor, user_id=42)

        envelope = agent.answer(QUESTION)

        assert envelope.outcome == "answer"
        assert envelope.summary == "Luka made 705 shots."
        assert envelope.sql == "SELECT COUNT(*) FROM nba.shot_detail"
        assert envelope.error is None
        assert envelope.result is not None
        assert envelope.result.rows == ((705,),)

        # One execute() call, attributed to the requested user.
        assert executor.calls == [("SELECT COUNT(*) FROM nba.shot_detail", 42)]
        assert envelope.tool_calls == 1

        # Four model-calling nodes (classify, draft_sql, critic, summarize).
        assert envelope.input_tokens == 40
        assert envelope.output_tokens == 20
        assert envelope.cache_read_input_tokens == 28
        # All four counters are real and DISJOINT: 3 per call x 4 calls.
        # This was 0 until 2026-08-07, which made cached runs look cheaper
        # than they were -- the regression test is that it is now non-zero.
        assert envelope.cache_creation_input_tokens == 12

        assert {t.node for t in envelope.node_timings} == {
            "classify", "draft_sql", "execute", "critic", "summarize",
        }
        assert envelope.total_ms >= 0
        assert envelope.model == "claude-sonnet-5"


class TestAbstention:
    def test_clarify_short_circuits_before_execute(self):
        model = FakeModelClient(
            scripts={"classify": [{"verdict": "ambiguous", "clarify_question": "Best by what measure?", "decline_reason": None}]}
        )
        executor = FakeExecutor(outcomes=[])
        agent = GraphAgent(model_client=model, executor=executor, user_id=1)

        envelope = agent.answer("Who is the best shooter?")

        assert envelope.outcome == "clarify"
        assert envelope.summary == "Best by what measure?"
        assert envelope.sql is None
        assert envelope.tool_calls == 0
        assert executor.calls == []

    def test_decline_short_circuits_before_execute(self):
        model = FakeModelClient(
            scripts={"classify": [{"verdict": "unanswerable", "clarify_question": None, "decline_reason": "No awards data."}]}
        )
        executor = FakeExecutor(outcomes=[])
        agent = GraphAgent(model_client=model, executor=executor, user_id=1)

        envelope = agent.answer("Who won MVP?")

        assert envelope.outcome == "decline"
        assert envelope.summary == "No awards data."
        assert envelope.tool_calls == 0


class TestRetriesCountAsToolCalls:
    def test_validator_rejection_retry_counts_two_tool_calls(self):
        model = FakeModelClient(
            scripts={
                "classify": [{"verdict": "answerable", "clarify_question": None, "decline_reason": None}],
                "draft_sql": [
                    {"sql": "UPDATE nba.shot_detail SET shot_made_flag = 1"},
                    {"sql": "SELECT COUNT(*) FROM nba.shot_detail"},
                ],
                "critic": [{"verdict": "ok", "feedback": ""}],
                "summarize": [{"summary": "705 shots."}],
            }
        )
        executor = FakeExecutor(
            outcomes=[
                ExecOutcome(status="validation_rejected", error="only SELECT statements are allowed"),
                ExecOutcome(status="ok", result=AgentExecResult(("n",), ((705,),))),
            ]
        )
        agent = GraphAgent(model_client=model, executor=executor, user_id=1)

        envelope = agent.answer(QUESTION)

        assert envelope.outcome == "answer"
        assert envelope.tool_calls == 2
        assert len(executor.calls) == 2

    def test_critic_rejection_retry_counts_two_tool_calls(self):
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
                ExecOutcome(status="ok", result=AgentExecResult(("n",), ((1000,),))),
                ExecOutcome(status="ok", result=AgentExecResult(("n",), ((705,),))),
            ]
        )
        agent = GraphAgent(model_client=model, executor=executor, user_id=1)

        envelope = agent.answer(QUESTION)

        assert envelope.tool_calls == 2


class TestFailureContract:
    def test_model_call_failure_is_returned_as_an_envelope_never_raised(self):
        """The one hard requirement: a flaky call scores as a miss, it does
        not abort the run. Matches ZeroShotBaseline.answer()'s contract."""
        model = FakeModelClient(scripts=_happy_scripts(), fail_on="classify")
        executor = FakeExecutor(outcomes=[])
        agent = GraphAgent(model_client=model, executor=executor, user_id=1)

        envelope = agent.answer(QUESTION)

        assert envelope.outcome == "answer"
        assert envelope.error is not None
        assert "simulated API failure" in envelope.error
        assert executor.calls == []

    def test_executor_failure_mid_run_is_returned_as_an_envelope_never_raised(self):
        model = FakeModelClient(scripts=_happy_scripts())
        executor = FakeExecutor(outcomes=[])  # raises AssertionError on first call

        agent = GraphAgent(model_client=model, executor=executor, user_id=1)
        envelope = agent.answer(QUESTION)

        assert envelope.error is not None


class TestUserIdRequired:
    def test_missing_user_id_raises_at_construction(self, monkeypatch):
        monkeypatch.delenv("EVAL_USER_ID", raising=False)
        model = FakeModelClient(scripts=_happy_scripts())
        executor = FakeExecutor(outcomes=[])

        with pytest.raises(RuntimeError, match="EVAL_USER_ID"):
            GraphAgent(model_client=model, executor=executor)

    def test_user_id_falls_back_to_env_var(self, monkeypatch):
        monkeypatch.setenv("EVAL_USER_ID", "7")
        model = FakeModelClient(scripts=_happy_scripts())
        executor = FakeExecutor(
            outcomes=[ExecOutcome(status="ok", result=AgentExecResult(("n",), ((705,),)))]
        )

        agent = GraphAgent(model_client=model, executor=executor)
        agent.answer(QUESTION)

        assert executor.calls == [("SELECT COUNT(*) FROM nba.shot_detail", 7)]
