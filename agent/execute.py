"""SQL execution via POST /api/internal/query.

The service never holds a database DSN (.agents/p4_agent.md "How the agent
executes SQL"): it reaches data only through this one authenticated HTTP call,
using AGENT_SERVICE_TOKEN and an explicit `user_id` for attribution. If you
find yourself importing psycopg2 or reading a `*_DATABASE_URL` env var here,
that is exactly the mistake this module exists to prevent.

Contract (owned by the concurrently-built Next.js endpoint, not this repo --
this module codes against it, does not define it):
    POST {base}/api/internal/query
    Authorization: Bearer <AGENT_SERVICE_TOKEN>
    body: {"sql": str, "userId": int}   -- no `source` field; the endpoint
                                            sets it server-side (hardcodes
                                            'agent') so a caller can't spoof
                                            its own provenance
    200 -> {columns, rows, rowCount, truncated, durationMs}
    400 -> {error[, durationMs]}   -- validator rejection (no durationMs) or a
                                       genuine SQL error the query ran into
                                       (durationMs present)
    429 -> rate limited
    504 -> statement timeout
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx

# Re-exported so callers can keep importing them from here; defined in
# agent/envelope.py, which is the single home for the agent's result types.
from .envelope import ExecResult, Row  # noqa: F401

# Error codes from /api/internal/query that no amount of redrafting can fix.
CALLER_ERROR_CODES = frozenset({"unknown_user", "invalid_user_id", "missing_sql"})


@dataclass(frozen=True)
class ExecOutcome:
    status: str  # "ok" | "timeout" | "error" | "validation_rejected" | "rate_limited"
    result: ExecResult | None = None
    error: str | None = None
    duration_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class InternalQueryExecutor:
    """POSTs to /api/internal/query.

    Constructing this never raises -- only `.execute()` does, and only if
    base_url/token are still unset by the time it's called -- so the service
    can start up and answer /health with no credentials configured at all.

    `transport` is the test seam: pass `httpx.MockTransport(handler)` to point
    at a stub server without a real network call, per the "make it easy to
    point at a stub server in tests" requirement.
    """

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        timeout_s: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = (base_url or os.environ.get("AGENT_API_BASE_URL", "")).rstrip("/")
        self._token = token or os.environ.get("AGENT_SERVICE_TOKEN", "")
        self._timeout_s = timeout_s
        self._transport = transport

    def execute(self, sql: str, user_id: int) -> ExecOutcome:
        if not self._base_url or not self._token:
            raise RuntimeError(
                "InternalQueryExecutor needs AGENT_API_BASE_URL and "
                "AGENT_SERVICE_TOKEN (or explicit base_url/token)."
            )

        started = time.monotonic()
        try:
            with httpx.Client(transport=self._transport, timeout=self._timeout_s) as client:
                response = client.post(
                    f"{self._base_url}/api/internal/query",
                    json={"sql": sql, "userId": user_id},
                    headers={"Authorization": f"Bearer {self._token}"},
                )
        except httpx.HTTPError as err:
            # Transport failure, not a query failure -- distinct from every
            # ExecOutcome status below, same convention as evals' executor.
            raise RuntimeError(f"/api/internal/query unreachable: {err}") from err

        elapsed_ms = (time.monotonic() - started) * 1000
        body = response.json() if response.content else {}

        if response.status_code == 200:
            return ExecOutcome(
                status="ok",
                result=ExecResult(
                    columns=tuple(body.get("columns", ())),
                    rows=tuple(tuple(row) for row in body.get("rows", ())),
                    truncated=bool(body.get("truncated", False)),
                ),
                duration_ms=body.get("durationMs", elapsed_ms),
            )
        if response.status_code == 429:
            return ExecOutcome(
                status="rate_limited",
                error=body.get("error", "rate limited"),
                duration_ms=body.get("durationMs", elapsed_ms),
            )
        if response.status_code == 504:
            return ExecOutcome(
                status="timeout",
                error=body.get("error", "statement timeout"),
                duration_ms=body.get("durationMs", elapsed_ms),
            )
        if response.status_code in (401, 403):
            # Configuration failure, not a query outcome -- raise rather than
            # scoring it as one, mirroring evals.harness.executors.
            raise RuntimeError(
                "/api/internal/query rejected the agent service token "
                f"({response.status_code}); this is a configuration failure."
            )

        # A caller error: the request itself was malformed or named a user that
        # does not exist. Redrafting the SQL cannot fix any of these, so they
        # must NOT look like a validator rejection -- otherwise the graph spends
        # its whole retry budget rewriting a query that was never the problem.
        # (This is not hypothetical: it was observed on the first end-to-end run,
        # where an unknown userId produced two pointless redrafts.)
        if body.get("code") in CALLER_ERROR_CODES:
            return ExecOutcome(
                status="config_error",
                error=body.get("error", f"HTTP {response.status_code}"),
                duration_ms=elapsed_ms,
            )

        # 400 and anything else land here. `durationMs` present means the
        # query ran and failed (a genuine SQL error); absent means the
        # validator rejected it before execution.
        status = "error" if "durationMs" in body else "validation_rejected"
        return ExecOutcome(
            status=status,
            error=body.get("error", f"HTTP {response.status_code}"),
            duration_ms=body.get("durationMs", elapsed_ms),
        )
