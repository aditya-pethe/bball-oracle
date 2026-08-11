"""CLI for eval runs.

    .venv/bin/python -m evals.run --agent baseline
    .venv/bin/python -m evals.run --agent baseline --model claude-opus-5 --effort medium
    .venv/bin/python -m evals.run --agent baseline --only t3-scoring-leaders-TRAP
    .venv/bin/python -m evals.run --agent baseline --executor internal-api
    .venv/bin/python -m evals.run --suite conversation --agent graph
    .venv/bin/python -m evals.run --suite conversation --agent baseline   # no-context control

Needs SANDBOX_RO_DATABASE_URL (gold SQL against the live 2023-24 data) and
Anthropic credentials. Never runs in CI: nondeterministic and it costs money.

`--suite conversation` runs the multi-turn set (evals/conversation-v0.yaml) and
reports follow-up accuracy and whole-conversation success separately. With
`--agent baseline` it wraps the single-turn zero-shot agent so every turn is
answered context-free -- the control that says how much carrying context actually
bought, which a follow-up accuracy number alone cannot.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES = REPO_ROOT / "evals" / "text2sql-v0.yaml"
DEFAULT_CONVERSATION_CASES = REPO_ROOT / "evals" / "conversation-v0.yaml"
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
    parser.add_argument("--suite", default="single", choices=["single", "conversation"])
    parser.add_argument(
        "--cases", default=None,
        help="Case file. Defaults to text2sql-v0.yaml, or conversation-v0.yaml "
             "with --suite conversation.",
    )
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

    conversational = args.suite == "conversation"
    cases_path = args.cases or str(
        DEFAULT_CONVERSATION_CASES if conversational else DEFAULT_CASES
    )
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

        from evals.harness.graph_agent import ConversationGraphAgent, GraphAgent

        agent_class = ConversationGraphAgent if conversational else GraphAgent
        agent = agent_class(model=args.model or DEFAULT_MODEL)
        agent_executor_name = "graph (agent/execute.py InternalQueryExecutor -> AGENT_API_BASE_URL)"
    else:
        agent_executor = build_executor(args.executor)
        agent = ZeroShotBaseline(
            agent_executor,
            model=args.model or DEFAULT_MODEL,
            effort=args.effort,
        )
        agent_executor_name = agent_executor.name
        if conversational:
            # The zero-shot baseline has no notion of a thread, so on the
            # multi-turn suite it becomes the no-context control: every turn
            # answered as if it were the first.
            from evals.harness.conversation_runner import StatelessConversationAgent

            agent = StatelessConversationAgent(agent)

    if conversational:
        return _run_conversation_suite(
            agent, args, cases_path, gold_executor, agent_executor_name
        )

    def progress(case, result):
        if case.is_execution_scored:
            mark = "PASS" if result.execution_correct else "FAIL"
        else:
            mark = "PASS" if result.outcome_correct else "FAIL"
        print(f"  [{mark}] {case.id}", file=sys.stderr, flush=True)

    print(f"running {agent.name} (agent SQL via {agent_executor_name})", file=sys.stderr)
    results = run_suite(
        agent,
        cases_path,
        gold_executor=gold_executor,
        config=RunnerConfig(pace_seconds=args.pace, on_case=progress),
        only=args.only,
    )

    meta = RunMeta(
        agent=agent.name,
        case_file=Path(cases_path).name,
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


def _run_conversation_suite(agent, args, cases_path, gold_executor, agent_executor_name) -> int:
    """The multi-turn path. Same metadata discipline and the same evals/runs/
    directory; a different report, because follow-up accuracy and whole-conversation
    success are the numbers this suite exists to produce."""
    from evals.harness.baseline import DEFAULT_MODEL
    from evals.harness.conversation_report import (
        build_conversation_run,
        format_conversation_summary,
    )
    from evals.harness.conversation_runner import run_conversation_suite
    from evals.harness.report import RunMeta, write_run

    def progress(case, result):
        marks = "".join("." if turn.passed else "X" for turn in result.turns)
        print(f"  [{marks}] {case.id}", file=sys.stderr, flush=True)

    print(f"running {agent.name} (agent SQL via {agent_executor_name})", file=sys.stderr)
    results = run_conversation_suite(
        agent,
        cases_path,
        gold_executor=gold_executor,
        pace_seconds=args.pace,
        only=args.only,
        on_conversation=progress,
    )

    meta = RunMeta(
        agent=agent.name,
        case_file=Path(cases_path).name,
        model=args.model or DEFAULT_MODEL,
        effort=None if args.agent == "graph" else args.effort,
        note=args.note,
    )
    print()
    print(format_conversation_summary(results, meta))

    if not args.no_store:
        path = write_run(build_conversation_run(results, meta), RUNS_DIR)
        print(f"\nrun written to {path.relative_to(REPO_ROOT)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
