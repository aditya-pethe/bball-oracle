"""Eval harness part B: runs cases against an agent and scores the results.

Unlike the comparator, this needs the live 2023-24 dataset -- gold SQL returns
nothing without it -- so it never runs in CI. Nightly or manual only, and it costs
real money per run.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .cases import EvalCase, load_cases
from .comparator import compare_results
from .envelope import Agent
from .executors import DirectExecutor, SqlExecutor
from .scoring import CaseResult


@dataclass
class RunnerConfig:
    # Seconds to sleep between cases. The user rate limit is 30 queries/60s
    # (web/lib/rate-limit.ts) and a full run issues roughly two per case, so the
    # default paces just under it. Set to 0 once the eval user has a raised limit.
    pace_seconds: float = 2.5
    fail_fast: bool = False
    on_case: object = None  # optional callable(case, result) for progress output


def run_case(
    case: EvalCase,
    agent: Agent,
    gold_executor: SqlExecutor,
) -> CaseResult:
    envelope = agent.answer(case.question)

    if not case.is_execution_scored:
        # Judgment-scored: the outcome is the whole signal here. Rubric grading of
        # the *wording* is a separate pass (an LLM judge or a human); scoring
        # clarify-vs-decline correctness is automatic and already meaningful.
        return CaseResult(case=case, envelope=envelope)

    assert case.gold_sql is not None  # guaranteed by the loader

    gold = gold_executor.execute(case.gold_sql)
    if not gold.ok:
        # The case file is broken, not the agent. Surfaced distinctly so a bad gold
        # query cannot masquerade as an agent regression.
        return CaseResult(
            case=case,
            envelope=envelope,
            gold_error=f"{gold.status}: {gold.error}",
        )

    if envelope.outcome != "answer" or envelope.result is None:
        return CaseResult(case=case, envelope=envelope)

    comparison = compare_results(
        gold.result.rows if gold.result else (),
        envelope.result.rows,
        order_matters=case.order_matters,
    )
    return CaseResult(case=case, envelope=envelope, comparison=comparison)


def run_suite(
    agent: Agent,
    case_file: str | Path,
    *,
    gold_executor: SqlExecutor | None = None,
    config: RunnerConfig | None = None,
    only: list[str] | None = None,
) -> list[CaseResult]:
    config = config or RunnerConfig()
    cases = load_cases(case_file)
    if only:
        wanted = set(only)
        cases = [c for c in cases if c.id in wanted]
        missing = wanted - {c.id for c in cases}
        if missing:
            raise ValueError(f"no such case id(s): {sorted(missing)}")

    executor = gold_executor or DirectExecutor()
    results: list[CaseResult] = []

    for index, case in enumerate(cases):
        result = run_case(case, agent, executor)
        results.append(result)

        if callable(config.on_case):
            config.on_case(case, result)

        if config.fail_fast and result.gold_error:
            raise RuntimeError(f"gold SQL failed on {case.id}: {result.gold_error}")

        if config.pace_seconds and index < len(cases) - 1:
            time.sleep(config.pace_seconds)

    return results
