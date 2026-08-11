"""Multi-turn metrics (.agents/p5_conversational_context.md "Harness changes").

Per-turn scoring is deliberately NOT reimplemented here: a turn is a `CaseResult`
and `scoring.score()` computes execution accuracy, abstention, latency and tokens
over any list of them. What this module adds is the slicing that only exists once
turns belong to conversations:

  - follow-ups scored apart from first turns, because a suite whose first turns
    carry it would report a conversational agent that cannot actually converse;
  - whole-conversation success, because a thread with one wrong turn is a wrong
    answer to the person having it, not a partial credit;
  - clarification continuation and context reset, the two behaviors Phase 5 exists
    to add and which no single-turn metric can see;
  - latency and tokens by turn index, which is how the context bounds get tuned
    from measurement rather than from the guess they started as.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .conversation_runner import ConversationResult
from .scoring import CaseResult, Metrics, score


def _pct(numerator: int, denominator: int) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


@dataclass
class ConversationMetrics:
    conversations: int = 0
    conversations_passed: int = 0

    turns: Metrics = field(default_factory=Metrics)
    first_turns: Metrics = field(default_factory=Metrics)
    followups: Metrics = field(default_factory=Metrics)
    by_turn_index: dict[int, Metrics] = field(default_factory=dict)

    reset_turns: int = 0
    reset_correct: int = 0

    clarification_continuations: int = 0
    clarification_continued: int = 0

    @property
    def conversation_success_rate(self) -> float:
        return _pct(self.conversations_passed, self.conversations)

    @property
    def context_reset_accuracy(self) -> float:
        """Of the turns that changed the subject, how many were answered without
        inheriting the previous topic's entities or filters?"""
        return _pct(self.reset_correct, self.reset_turns)

    @property
    def clarification_continuation_rate(self) -> float:
        """Of the turns answering a clarification the agent asked for, how many
        actually continued the original task?

        A narrower follow-up clarification counts: the plan allows an agent to ask
        one more specific question rather than guess. What does not count is a
        decline or an error — those abandon a task the user already tried twice to
        get answered. Whether a narrower clarification was *justified* is a rubric
        judgment for a human, so this metric is a ceiling, not a grade; the same
        turns are also execution-scored in `turns`, which is the stricter number.

        The denominator counts turns where a clarification was ACTUALLY pending —
        the case expects one AND the agent asked one. An agent that answered the
        ambiguous turn instead never exercised continuation at all, and scoring
        that as a continuation failure would blame this metric for a miss that
        outcome accuracy already records. A shrinking denominator is the signal,
        and the report prints it next to the rate for exactly that reason.
        """
        return _pct(self.clarification_continued, self.clarification_continuations)


def _continued(result: CaseResult) -> bool:
    if result.envelope.error is not None:
        return False
    return result.envelope.outcome in ("answer", "clarify")


def score_conversations(results: list[ConversationResult]) -> ConversationMetrics:
    metrics = ConversationMetrics(conversations=len(results))

    all_turns: list[CaseResult] = []
    first_turns: list[CaseResult] = []
    followups: list[CaseResult] = []
    by_index: dict[int, list[CaseResult]] = {}

    for conversation in results:
        if conversation.passed:
            metrics.conversations_passed += 1

        for index, turn in enumerate(conversation.turns):
            all_turns.append(turn)
            (first_turns if index == 0 else followups).append(turn)
            by_index.setdefault(index, []).append(turn)

            case = conversation.case.turns[index]
            if case.reset:
                metrics.reset_turns += 1
                if turn.passed:
                    metrics.reset_correct += 1
            actually_pending = (
                case.follows_clarify
                and index > 0
                and conversation.turns[index - 1].envelope.outcome == "clarify"
            )
            if actually_pending:
                metrics.clarification_continuations += 1
                if _continued(turn):
                    metrics.clarification_continued += 1

    metrics.turns = score(all_turns)
    metrics.first_turns = score(first_turns)
    metrics.followups = score(followups)
    metrics.by_turn_index = {index: score(rows) for index, rows in sorted(by_index.items())}
    return metrics
