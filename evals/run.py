"""CLI for eval runs.

    .venv/bin/python -m evals.run --agent baseline
    .venv/bin/python -m evals.run --agent baseline --model claude-opus-5 --effort medium
    .venv/bin/python -m evals.run --agent baseline --only t3-scoring-leaders-TRAP
    .venv/bin/python -m evals.run --agent baseline --executor internal-api

Needs SANDBOX_RO_DATABASE_URL (gold SQL against the live 2023-24 data) and
Anthropic credentials. Never runs in CI: nondeterministic and it costs money.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES = REPO_ROOT / "evals" / "text2sql-v0.yaml"
RUNS_DIR = REPO_ROOT / "evals" / "runs"


def load_dotenv(path: Path) -> None:
    """Minimal .env reader so a run does not need a pre-exported shell."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def build_executor(kind: str):
    from evals.harness.executors import DirectExecutor, InternalApiExecutor

    if kind == "direct":
        return DirectExecutor()
    if kind == "internal-api":
        return InternalApiExecutor()
    raise ValueError(f"unknown executor {kind!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.run")
    parser.add_argument("--agent", default="baseline", choices=["baseline", "graph"])
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--model", default=None)
    parser.add_argument("--effort", default=None,
                        choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument(
        "--executor", default="direct", choices=["direct", "internal-api"],
        help="How AGENT SQL is executed. Gold SQL always runs direct. "
             "Use internal-api once /api/internal/query exists (step 4).",
    )
    parser.add_argument("--only", nargs="*", default=None, help="Case ids to run.")
    parser.add_argument("--pace", type=float, default=2.5,
                        help="Seconds between cases; keeps under the 30/60s rate limit.")
    parser.add_argument("--no-store", action="store_true", help="Skip writing evals/runs/.")
    parser.add_argument("--note", default=None, help="Free-text note recorded in the run file.")
    args = parser.parse_args(argv)

    load_dotenv(REPO_ROOT / ".env")

    from evals.harness.baseline import DEFAULT_MODEL, ZeroShotBaseline
    from evals.harness.report import RunMeta, build_run, format_summary, write_run
    from evals.harness.runner import RunnerConfig, run_suite

    gold_executor = build_executor("direct")

    if args.agent == "graph":
        # SQL execution is not a harness SqlExecutor here -- it is baked into
        # the graph itself (agent/nodes/execute.py -> agent/execute.py's
        # InternalQueryExecutor), which reads AGENT_API_BASE_URL /
        # AGENT_SERVICE_TOKEN directly from the environment. --executor
        # governs only the baseline's SQL path and does not apply.
        if args.executor != "direct":
            print("--executor is ignored for --agent graph (see evals/run.py)", file=sys.stderr)
        if args.effort:
            print("--effort is ignored for --agent graph (per-node model config "
                  "lives in agent/models.py)", file=sys.stderr)

        from evals.harness.graph_agent import GraphAgent

        agent = GraphAgent(model=args.model or DEFAULT_MODEL)
        agent_executor_name = "graph (agent/execute.py InternalQueryExecutor -> AGENT_API_BASE_URL)"
    else:
        agent_executor = build_executor(args.executor)
        agent = ZeroShotBaseline(
            agent_executor,
            model=args.model or DEFAULT_MODEL,
            effort=args.effort,
        )
        agent_executor_name = agent_executor.name

    def progress(case, result):
        if case.is_execution_scored:
            mark = "PASS" if result.execution_correct else "FAIL"
        else:
            mark = "PASS" if result.outcome_correct else "FAIL"
        print(f"  [{mark}] {case.id}", file=sys.stderr, flush=True)

    print(f"running {agent.name} (agent SQL via {agent_executor_name})", file=sys.stderr)
    results = run_suite(
        agent,
        args.cases,
        gold_executor=gold_executor,
        config=RunnerConfig(pace_seconds=args.pace, on_case=progress),
        only=args.only,
    )

    meta = RunMeta(
        agent=agent.name,
        case_file=Path(args.cases).name,
        model=args.model or DEFAULT_MODEL,
        effort=None if args.agent == "graph" else args.effort,
        note=args.note,
    )
    print()
    print(format_summary(results, meta))

    if not args.no_store:
        run = build_run(results, meta)
        path = write_run(run, RUNS_DIR)
        print(f"\nrun written to {path.relative_to(REPO_ROOT)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
