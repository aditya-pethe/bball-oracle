"""Turning per-case outcomes into the metrics the phase actually decides on.

Split from comparator.py because they answer different questions: the comparator
asks "are these two result sets the same", this module asks "what does that tell us
about the agent". The second is where the weighting judgments live.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

from .cases import EvalCase
from .comparator import Comparison
from .envelope import AgentEnvelope

ABSTENTIONS = frozenset({"clarify", "decline"})


@dataclass
class CaseResult:
    case: EvalCase
    envelope: AgentEnvelope
    comparison: Comparison | None = None
    gold_error: str | None = None

    @property
    def abstained(self) -> bool:
        return self.envelope.outcome in ABSTENTIONS

    @property
    def outcome_correct(self) -> bool:
        """Did the agent pick the right *kind* of response?

        Exact match by default, so clarifying on an unanswerable question counts
        as wrong even though both are abstentions -- "I need more detail" and
        "the data cannot answer this" send the user down different paths. A case
        may widen this via `also_accepts` where more than one response is
        genuinely defensible.
        """
        return self.envelope.outcome in self.case.accepted_outcomes

    @property
    def execution_correct(self) -> bool:
        """Execution accuracy for this case. Only meaningful when expects == answer."""
        if not self.case.is_execution_scored:
            return False
        if self.envelope.outcome != "answer":
            return False
        return self.comparison is not None and self.comparison.matches

    @property
    def passed(self) -> bool:
        """One number for "did this case go right", whichever way it is scored.

        Execution-scored cases pass on matching rows; judgment-scored cases pass on
        picking the right kind of response. Lives here rather than in report.py
        because whole-conversation success (conversation_scoring.py) needs the same
        definition, and two copies of it would eventually disagree.
        """
        return self.execution_correct if self.case.is_execution_scored else self.outcome_correct

    @property
    def produced_valid_sql(self) -> bool:
        return (
            self.envelope.sql is not None
            and self.envelope.result is not None
            and self.envelope.error is None
        )


def _pct(numerator: int, denominator: int) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


@dataclass
class Metrics:
    total: int = 0

    answerable: int = 0
    execution_correct: int = 0
    valid_sql: int = 0
    # Right *kind* of response (answer/clarify/decline), across every case rather
    # than only the abstention ones. The multi-turn suite reports this per turn.
    outcome_correct: int = 0

    should_abstain: int = 0
    did_abstain_when_should: int = 0
    did_abstain_total: int = 0
    abstention_outcome_exact: int = 0
    false_abstentions: int = 0

    gold_errors: int = 0
    tool_calls: list[int] = field(default_factory=list)
    latencies_ms: list[float] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def execution_accuracy(self) -> float:
        return _pct(self.execution_correct, self.answerable)

    @property
    def valid_sql_rate(self) -> float:
        return _pct(self.valid_sql, self.answerable)

    @property
    def outcome_accuracy(self) -> float:
        """How often the agent picked the right kind of response at all — before
        asking whether the rows were also right."""
        return _pct(self.outcome_correct, self.total)

    @property
    def abstention_recall(self) -> float:
        """Of the cases that should have been abstained on, how many were?"""
        return _pct(self.did_abstain_when_should, self.should_abstain)

    @property
    def abstention_precision(self) -> float:
        """Of the abstentions the agent made, how many were warranted?"""
        return _pct(self.did_abstain_when_should, self.did_abstain_total)

    @property
    def abstention_exact_rate(self) -> float:
        """Recall, but requiring clarify-vs-decline to be right too."""
        return _pct(self.abstention_outcome_exact, self.should_abstain)

    @property
    def false_abstention_rate(self) -> float:
        """Of answerable questions, how many did the agent duck?

        The metric that keeps abstention honest: without it, an agent that clarifies
        on everything scores perfect recall.
        """
        return _pct(self.false_abstentions, self.answerable)

    @property
    def total_input_tokens(self) -> int:
        """The three input counters are disjoint; this is real prompt volume."""
        return (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    @property
    def cache_hit_rate(self) -> float:
        """Share of input tokens served from cache. Zero means caching is not
        firing -- most likely the prefix is under the model's minimum."""
        return _pct(self.cache_read_input_tokens, self.total_input_tokens)

    @property
    def mean_tool_calls(self) -> float:
        return sum(self.tool_calls) / len(self.tool_calls) if self.tool_calls else 0.0

    @property
    def latency_p50(self) -> float:
        return median(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def latency_p95(self) -> float:
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        # Nearest-rank p95; with 25 cases the interpolating variants are noise.
        index = max(0, min(len(ordered) - 1, round(0.95 * len(ordered)) - 1))
        return ordered[index]


def score(results: list[CaseResult]) -> Metrics:
    metrics = Metrics(total=len(results))

    for result in results:
        metrics.tool_calls.append(result.envelope.tool_calls)
        metrics.latencies_ms.append(result.envelope.total_ms)
        metrics.input_tokens += result.envelope.input_tokens
        metrics.output_tokens += result.envelope.output_tokens
        metrics.cache_creation_input_tokens += result.envelope.cache_creation_input_tokens
        metrics.cache_read_input_tokens += result.envelope.cache_read_input_tokens

        if result.gold_error:
            metrics.gold_errors += 1

        if result.outcome_correct:
            metrics.outcome_correct += 1

        if result.abstained:
            metrics.did_abstain_total += 1

        if result.case.is_execution_scored:
            metrics.answerable += 1
            if result.execution_correct:
                metrics.execution_correct += 1
            if result.produced_valid_sql:
                metrics.valid_sql += 1
            if result.abstained:
                metrics.false_abstentions += 1
        else:
            metrics.should_abstain += 1
            if result.abstained:
                metrics.did_abstain_when_should += 1
            if result.outcome_correct:
                metrics.abstention_outcome_exact += 1

    return metrics


def score_by_tier(results: list[CaseResult]) -> dict[int, Metrics]:
    tiers: dict[int, list[CaseResult]] = {}
    for result in results:
        tiers.setdefault(result.case.tier, []).append(result)
    return {tier: score(rows) for tier, rows in sorted(tiers.items())}
