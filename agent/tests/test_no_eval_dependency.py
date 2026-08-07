"""The deployed service must not depend on the eval harness.

This is a packaging invariant, not a style preference. `agent/Dockerfile` copies
only `agent/`, so if a service module ever imports `evals.*` the container will
ImportError at startup -- and it will do so in production, on a deploy, not in
CI. This test moves that failure to the cheapest possible place.

It also guards the direction of the dependency. The harness measures the agent,
so evals -> agent is correct and expected; agent -> evals is backwards, and it
drags PyYAML, psycopg2 and the whole measurement layer into an image whose whole
security story is that it holds no database credentials.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Every module that ends up in the container.
SERVICE_MODULES = [
    "agent.service",
    "agent.graph",
    "agent.state",
    "agent.execute",
    "agent.envelope",
    "agent.llm",
    "agent.models",
    "agent.schema_prompt",
    "agent.nodes.classify",
    "agent.nodes.clarify",
    "agent.nodes.critic",
    "agent.nodes.decline",
    "agent.nodes.draft_sql",
    "agent.nodes.execute",
    "agent.nodes.summarize",
]


def test_service_import_tree_never_touches_evals():
    # A subprocess, so an `evals` module already imported by another test in
    # this session cannot mask the failure.
    script = (
        "import sys\n"
        + "".join(f"import {m}\n" for m in SERVICE_MODULES)
        + "leaked = sorted(m for m in sys.modules if m == 'evals' or m.startswith('evals.'))\n"
        "print(','.join(leaked))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    leaked = result.stdout.strip()
    assert leaked == "", (
        f"service modules imported the eval harness: {leaked}. "
        "Move the shared type into agent/envelope.py instead -- the container "
        "does not ship evals/."
    )


def test_service_does_not_import_a_postgres_driver():
    """The container holds no DSN, so it has no business holding a driver.

    A psycopg2 import appearing here would mean someone gave the service a
    direct database path, bypassing /api/internal/query and with it the
    validator, the row cap, the statement timeout and the audit log.
    """
    script = (
        "import sys\n"
        + "".join(f"import {m}\n" for m in SERVICE_MODULES)
        + "drivers = sorted(m for m in sys.modules "
        "if m.split('.')[0] in {'psycopg2', 'psycopg', 'asyncpg', 'sqlalchemy'})\n"
        "print(','.join(drivers))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", (
        f"service imported a database driver: {result.stdout.strip()}"
    )


def test_requirements_do_not_carry_harness_or_test_deps():
    # Requirement lines only -- the comments in that file legitimately mention
    # psycopg2 and PyYAML while explaining why they are absent.
    lines = [
        line.split("#", 1)[0].strip().lower()
        for line in (REPO_ROOT / "agent" / "requirements.txt").read_text().splitlines()
    ]
    packages = [line for line in lines if line]

    for forbidden in ("psycopg2", "pyyaml", "pytest"):
        offending = [p for p in packages if p.startswith(forbidden)]
        assert not offending, (
            f"{offending} is in agent/requirements.txt and would ship to production"
        )
