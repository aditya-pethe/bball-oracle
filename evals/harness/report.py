"""Run output: a terminal summary to read now, a JSON record to diff later.

Runs are stored rather than printed-and-forgotten because the phase's core claims
are comparative -- "the critic node earns its latency" only means something against
a previous run's numbers.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .scoring import CaseResult, Metrics, score, score_by_tier


@dataclass(frozen=True)
class RunMeta:
    agent: str
    case_file: str
    model: str | None = None
    effort: str | None = None
    note: str | None = None


def _metrics_dict(metrics: Metrics) -> dict[str, Any]:
    return {
        "total": metrics.total,
        "answerable": metrics.answerable,
        "execution_accuracy": round(metrics.execution_accuracy, 4),
        "outcome_accuracy": round(metrics.outcome_accuracy, 4),
        "valid_sql_rate": round(metrics.valid_sql_rate, 4),
        "abstention_recall": round(metrics.abstention_recall, 4),
        "abstention_precision": round(metrics.abstention_precision, 4),
        "abstention_exact_rate": round(metrics.abstention_exact_rate, 4),
        "false_abstention_rate": round(metrics.false_abstention_rate, 4),
        "mean_tool_calls": round(metrics.mean_tool_calls, 2),
        "latency_p50_ms": round(metrics.latency_p50, 1),
        "latency_p95_ms": round(metrics.latency_p95, 1),
        "input_tokens": metrics.input_tokens,
        "cache_creation_input_tokens": metrics.cache_creation_input_tokens,
        "total_input_tokens": metrics.total_input_tokens,
        "cache_hit_rate": round(metrics.cache_hit_rate, 4),
        "output_tokens": metrics.output_tokens,
        "cache_read_input_tokens": metrics.cache_read_input_tokens,
        "gold_errors": metrics.gold_errors,
    }


def _case_dict(result: CaseResult) -> dict[str, Any]:
    env = result.envelope
    return {
        "id": result.case.id,
        "tier": result.case.tier,
        "trap": result.case.trap,
        "expects": result.case.expects,
        "outcome": env.outcome,
        "outcome_correct": result.outcome_correct,
        "execution_correct": result.execution_correct if result.case.is_execution_scored else None,
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
        "node_timings": [
            {"node": t.node, "duration_ms": round(t.duration_ms, 1)} for t in env.node_timings
        ],
    }


def build_run(results: list[CaseResult], meta: RunMeta) -> dict[str, Any]:
    return {
        "meta": {
            "agent": meta.agent,
            "case_file": meta.case_file,
            "model": meta.model,
            "effort": meta.effort,
            "note": meta.note,
            "run_at": datetime.now(timezone.utc).isoformat(),
        },
        "metrics": _metrics_dict(score(results)),
        "by_tier": {
            str(tier): _metrics_dict(tier_metrics)
            for tier, tier_metrics in score_by_tier(results).items()
        },
        "cases": [_case_dict(result) for result in results],
    }


def write_run(run: dict[str, Any], directory: str | Path) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    agent = run["meta"]["agent"].replace("/", "_")
    path = directory / f"{stamp}-{agent}.json"
    path.write_text(json.dumps(run, indent=2, default=str) + "\n")
    return path


def format_summary(results: list[CaseResult], meta: RunMeta) -> str:
    metrics = score(results)
    lines: list[str] = []

    header = f"{meta.agent} on {meta.case_file}"
    if meta.model:
        header += f"  [{meta.model}{'/' + meta.effort if meta.effort else ''}]"
    lines.append(header)
    lines.append("=" * len(header))
    lines.append("")

    lines.append(f"  execution accuracy     {metrics.execution_accuracy:>6.1%}  "
                 f"({metrics.execution_correct}/{metrics.answerable})")
    lines.append(f"  valid-SQL rate         {metrics.valid_sql_rate:>6.1%}")
    lines.append(f"  abstention recall      {metrics.abstention_recall:>6.1%}  "
                 f"({metrics.did_abstain_when_should}/{metrics.should_abstain})")
    lines.append(f"  abstention precision   {metrics.abstention_precision:>6.1%}")
    lines.append(f"  abstention exact       {metrics.abstention_exact_rate:>6.1%}  "
                 f"(clarify vs decline correct)")
    lines.append(f"  false-abstention rate  {metrics.false_abstention_rate:>6.1%}  "
                 f"({metrics.false_abstentions}/{metrics.answerable} answerable ducked)")
    lines.append("")
    lines.append(f"  latency p50 / p95      {metrics.latency_p50/1000:.1f}s / "
                 f"{metrics.latency_p95/1000:.1f}s")
    lines.append(f"  mean tool calls        {metrics.mean_tool_calls:.2f}")
    lines.append(f"  tokens in / out        {metrics.total_input_tokens} / {metrics.output_tokens}")
    lines.append(f"  cache hit rate         {metrics.cache_hit_rate:>6.1%}  "
                 f"(read {metrics.cache_read_input_tokens}, "
                 f"wrote {metrics.cache_creation_input_tokens}, "
                 f"uncached {metrics.input_tokens})")
    if metrics.gold_errors:
        lines.append(f"  !! gold SQL errors     {metrics.gold_errors} "
                     f"-- the case file is broken, not the agent")
    lines.append("")

    failures = [r for r in results if not _passed(r)]
    if failures:
        lines.append(f"failures ({len(failures)}/{metrics.total})")
        lines.append("-" * 40)
        for result in failures:
            lines.append(f"  {result.case.id}  [tier {result.case.tier}"
                         f"{', TRAP' if result.case.trap else ''}]")
            lines.append(f"    expected {result.case.expects}, got {result.envelope.outcome}")
            if result.comparison and result.comparison.mismatch:
                lines.append(f"    {result.comparison.mismatch.kind}: "
                             f"{result.comparison.mismatch.detail}")
            if result.envelope.error:
                lines.append(f"    error: {result.envelope.error}")
            if result.gold_error:
                lines.append(f"    GOLD ERROR: {result.gold_error}")
    else:
        lines.append("all cases passed")

    return "\n".join(lines)


def _passed(result: CaseResult) -> bool:
    return result.passed
