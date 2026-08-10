"""Fakes shared across the agent test suite.

No database, no API key, no network -- everything here is a scripted stand-in
for the real `ModelClient` / SQL executor, so agent/tests/test_graph.py and
agent/tests/test_service.py can exercise the real graph structure (edges,
retry caps) deterministically.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from agent.execute import ExecOutcome
from agent.llm import ModelReply


@dataclass
class FakeModelClient:
    """Scripted model client. `scripts[node]` is a list of payload dicts
    consumed in call order; a node called more times than it has scripted
    replies raises -- deliberately, since that means the test under-specified
    how many times the graph should reach that node.

    `messages_log` mirrors `calls` but keeps the actual `messages` argument
    each call was made with, so node-level tests can assert on prompt content
    (e.g. "a retry includes the prior SQL"), not just call counts.
    """

    scripts: dict[str, list[dict]]
    calls: list[str] = field(default_factory=list)
    conversation_message_ids: list[int | None] = field(default_factory=list)
    messages_log: list[list[dict]] = field(default_factory=list)

    def complete(self, node: str, *, messages: list[dict], schema: dict) -> ModelReply:
        self.calls.append(node)
        self.messages_log.append(messages)
        queue = self.scripts.get(node)
        if not queue:
            raise AssertionError(f"no more scripted replies for node {node!r}")
        payload = queue.pop(0)
        return ModelReply(
            payload=payload, input_tokens=10, output_tokens=5,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        )


@dataclass
class FakeExecutor:
    """Scripted executor. `outcomes` is consumed in call order, same
    over-call-raises convention as FakeModelClient."""

    outcomes: list[ExecOutcome]
    calls: list[str] = field(default_factory=list)
    conversation_message_ids: list[int | None] = field(default_factory=list)

    def execute(
        self,
        sql: str,
        user_id: int,
        conversation_message_id: int | None = None,
    ) -> ExecOutcome:
        self.conversation_message_ids.append(conversation_message_id)
        self.calls.append(sql)
        if not self.outcomes:
            raise AssertionError("no more scripted executor outcomes")
        return self.outcomes.pop(0)
