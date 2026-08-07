"""The agent's own result types.

These live here, in the deployed package, rather than in the eval harness --
and the direction matters. The harness *evaluates* the agent, so it may depend
on the agent; the agent must not depend on the thing that measures it. When
these types lived in `evals.harness.envelope`, `agent/state.py` and
`agent/graph.py` imported them, which meant the production container had to ship
the eval harness plus PyYAML and psycopg2 to get two dataclasses.

`AgentEnvelope` is the response contract, and it is deliberately one shape
shared by three consumers: the SSE `done` event the UI renders, the object the
eval harness scores, and the record persisted to `app.conversation_message`.
Designing it once is why the UI could reuse `ResultsArea` unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Outcome = Literal["answer", "clarify", "decline"]

Row = tuple[object, ...]


@dataclass(frozen=True)
class ExecResult:
    """One query's result set.

    Column names are carried for rendering but are deliberately NOT part of
    correctness: the eval comparator ignores them, because `COUNT(*) AS shots`
    and `COUNT(*) AS n` are the same answer.
    """

    columns: tuple[str, ...]
    rows: tuple[Row, ...]
    truncated: bool = False

    @property
    def row_count(self) -> int:
        return len(self.rows)


@dataclass(frozen=True)
class NodeTiming:
    node: str
    duration_ms: float


@dataclass
class AgentEnvelope:
    """One agent response.

    `outcome` is independent of whether `sql` is present: an agent that emits a
    clarifying question *and* speculative SQL has still clarified, and one that
    declines while quietly returning rows has not.
    """

    outcome: Outcome
    summary: str
    sql: str | None = None
    result: ExecResult | None = None
    error: str | None = None

    # Instrumentation.
    #
    # The three input counters are DISJOINT, not overlapping: `input_tokens` is
    # the uncached remainder only, so total prompt size is the sum of all three.
    # Recording only `input_tokens` makes a cached request look ~100x cheaper
    # than it is (a 2.2k-token prefix reports as ~17) -- measured, not
    # hypothetical; it was a real bug caught by the first live eval run.
    node_timings: list[NodeTiming] = field(default_factory=list)
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    total_ms: float = 0.0
    model: str | None = None

    @property
    def total_input_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    def timing_for(self, node: str) -> float:
        return sum(t.duration_ms for t in self.node_timings if t.node == node)
