"""Output for a multi-turn run: a terminal summary now, a JSON record to diff later.

Same metadata discipline and the same `evals/runs/` directory as the single-turn
report (`write_run` is reused verbatim), because the comparison that matters most is
across runs of the same suite. The single-turn `format_summary` is left untouched:
`text2sql-v0.yaml` results have to stay directly comparable with the Phase 4
baseline, and a report that quietly changed shape would break that comparison
without breaking any test.

First-turn accuracy is printed next to follow-up accuracy on purpose. Carrying
conversation context is a change to every prompt, including the first turn's, and
the first-turn number is the regression check on that: it should track the
single-turn suite, and a drop there means context cost something it should not have.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .conversation_runner import ConversationResult
from .conversation_scoring import ConversationMetrics, score_conversations
from .report import RunMeta, _metrics_dict
from .scoring import CaseResult


def _turn_dict(result: CaseResult) -> dict[str, Any]:
    case = result.case
    env = result.envelope
    return {
        "id": case.id,
        "index": getattr(case, "index", None),
        "question": case.question,
        "expects": case.expects,
        "reset": getattr(case, "reset", False),
        "follows_clarify": getattr(case, "follows_clarify", False),
        "outcome": env.outcome,
        "outcome_correct": result.outcome_correct,
        "execution_correct": result.execution_correct if case.is_execution_scored else None,
        "passed": result.passed,
        "sql": env.sql,
        "summary": env.summary,
        "error": env.error,
        "gold_error": result.gold_error,
        "mismatch": (
            {"kind": result.comparison.mismatch.kind, "detail": result.comparison.mismatch.detail}
            if result.comparison and result.comparison.mismatch
            else None
        ),
        "tool_calls": env.tool_calls,
        "total_ms": round(env.total_ms, 1),
        "input_tokens": env.input_tokens,
        "output_tokens": env.output_tokens,
        "total_input_tokens": env.total_input_tokens,
    }


def _conversation_dict(result: ConversationResult) -> dict[str, Any]:
    return {
        "id": result.case.id,
        "tier": result.case.tier,
        "notes": result.case.notes,
        "passed": result.passed,
        "turns": [_turn_dict(turn) for turn in result.turns],
    }


def _conversation_metrics_dict(metrics: ConversationMetrics) -> dict[str, Any]:
    return {
        "conversations": metrics.conversations,
        "conversations_passed": metrics.conversations_passed,
        "conversation_success_rate": round(metrics.conversation_success_rate, 4),
        "turn_execution_accuracy": round(metrics.turns.execution_accuracy, 4),
        "followup_execution_accuracy": round(metrics.followups.execution_accuracy, 4),
        "first_turn_execution_accuracy": round(metrics.first_turns.execution_accuracy, 4),
        "outcome_accuracy": round(metrics.turns.outcome_accuracy, 4),
        "clarification_continuations": metrics.clarification_continuations,
        "clarification_continuation_rate": round(metrics.clarification_continuation_rate, 4),
        "reset_turns": metrics.reset_turns,
        "context_reset_accuracy": round(metrics.context_reset_accuracy, 4),
        "turns": _metrics_dict(metrics.turns),
        "followups": _metrics_dict(metrics.followups),
        "first_turns": _metrics_dict(metrics.first_turns),
    }


def build_conversation_run(results: list[ConversationResult], meta: RunMeta) -> dict[str, Any]:
    metrics = score_conversations(results)
    return {
        "meta": {
            "suite": "conversation",
            "agent": meta.agent,
            "case_file": meta.case_file,
            "model": meta.model,
            "effort": meta.effort,
            "note": meta.note,
            "run_at": datetime.now(timezone.utc).isoformat(),
        },
        "metrics": _conversation_metrics_dict(metrics),
        "by_turn_index": {
            str(index): _metrics_dict(turn_metrics)
            for index, turn_metrics in metrics.by_turn_index.items()
        },
        "conversations": [_conversation_dict(result) for result in results],
    }


def format_conversation_summary(results: list[ConversationResult], meta: RunMeta) -> str:
    metrics = score_conversations(results)
    lines: list[str] = []

    header = f"{meta.agent} on {meta.case_file}"
    if meta.model:
        header += f"  [{meta.model}{'/' + meta.effort if meta.effort else ''}]"
    lines.append(header)
    lines.append("=" * len(header))
    lines.append("")

    lines.append(
        f"  conversation success rate      {metrics.conversation_success_rate:>6.1%}  "
        f"({metrics.conversations_passed}/{metrics.conversations} whole conversations)"
    )
    lines.append(
        f"  turn execution accuracy        {metrics.turns.execution_accuracy:>6.1%}  "
        f"({metrics.turns.execution_correct}/{metrics.turns.answerable})"
    )
    lines.append(
        f"  follow-up execution accuracy   {metrics.followups.execution_accuracy:>6.1%}  "
        f"({metrics.followups.execution_correct}/{metrics.followups.answerable})"
    )
    lines.append(
        f"  first-turn execution accuracy  {metrics.first_turns.execution_accuracy:>6.1%}  "
        f"(regression check against the single-turn suite)"
    )
    lines.append(
        f"  outcome accuracy               {metrics.turns.outcome_accuracy:>6.1%}  "
        f"({metrics.turns.outcome_correct}/{metrics.turns.total} turns)"
    )
    lines.append(
        f"  clarification continuation     {metrics.clarification_continuation_rate:>6.1%}  "
        f"({metrics.clarification_continued}/{metrics.clarification_continuations})"
    )
    lines.append(
        f"  context-reset accuracy         {metrics.context_reset_accuracy:>6.1%}  "
        f"({metrics.reset_correct}/{metrics.reset_turns})"
    )
    lines.append(
        f"  false-abstention rate          {metrics.turns.false_abstention_rate:>6.1%}  "
        f"({metrics.turns.false_abstentions}/{metrics.turns.answerable} answerable ducked)"
    )
    lines.append("")

    # The cost of carrying context, which is what the bounds get tuned against.
    lines.append("  by turn index          latency p50    tokens in   cache hit")
    for index, turn_metrics in metrics.by_turn_index.items():
        lines.append(
            f"    turn {index + 1:<2}              "
            f"{turn_metrics.latency_p50 / 1000:>6.1f}s  "
            f"{turn_metrics.total_input_tokens:>11}  "
            f"{turn_metrics.cache_hit_rate:>9.1%}"
        )
    lines.append("")

    if metrics.turns.gold_errors:
        lines.append(
            f"  !! gold SQL errors     {metrics.turns.gold_errors} "
            f"-- the case file is broken, not the agent"
        )
        lines.append("")

    failed = [r for r in results if not r.passed]
    if failed:
        lines.append(f"failed conversations ({len(failed)}/{metrics.conversations})")
        lines.append("-" * 40)
        for conversation in failed:
            lines.append(f"  {conversation.case.id}  [tier {conversation.case.tier}]")
            for turn in conversation.turns:
                if turn.passed:
                    continue
                case = turn.case
                lines.append(
                    f"    turn {getattr(case, 'index', 0) + 1}: {case.question}"
                )
                lines.append(
                    f"      expected {case.expects}, got {turn.envelope.outcome}"
                )
                if turn.comparison and turn.comparison.mismatch:
                    lines.append(
                        f"      {turn.comparison.mismatch.kind}: "
                        f"{turn.comparison.mismatch.detail}"
                    )
                if turn.envelope.error:
                    lines.append(f"      error: {turn.envelope.error}")
                if turn.gold_error:
                    lines.append(f"      GOLD ERROR: {turn.gold_error}")
    else:
        lines.append("all conversations passed")

    return "\n".join(lines)
