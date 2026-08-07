"""What the harness evaluates.

The result types now live in `agent.envelope` and are re-exported here: the
harness depends on the agent (it measures it), never the reverse. That
direction is what keeps the deployed container free of the eval harness --
see agent/envelope.py.

`Agent` stays here because it is a harness concern: it defines what the
harness is able to score, not anything the service needs to know.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent.envelope import (  # noqa: F401  -- re-exported for harness callers
    AgentEnvelope,
    ExecResult,
    NodeTiming,
    Outcome,
    Row,
)


@runtime_checkable
class Agent(Protocol):
    """The one method the harness calls.

    Implementations must not raise for ordinary failure (bad SQL, a rejected
    validation, an LLM error): return an envelope with `error` set instead. A raised
    exception is reserved for harness-level breakage -- a missing API key, an
    unreachable service -- which should abort the run rather than score as a miss.
    """

    name: str

    def answer(self, question: str) -> AgentEnvelope: ...
