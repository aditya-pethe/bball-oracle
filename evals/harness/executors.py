"""How the harness gets SQL executed.

Two implementations behind one interface, and the split is load-bearing rather than
speculative:

* `DirectExecutor` opens its own read-only `sandbox_ro` connection. This is the
  correct executor for **gold SQL** permanently -- gold is reference data, not a
  system under test, and routing it through the app would double query volume
  against the rate limiter for no fidelity gain.

* `InternalApiExecutor` posts to `/api/internal/query`, exercising the real chain
  (validator -> sandbox_ro -> timeout/row cap -> query_log). This is the correct
  executor for **agent SQL**, per the resolution in .agents/p4_agent.md: a
  regression in the safety chain should surface as an eval failure.

The seam also resolves an ordering problem in the phase plan. Step 2 (baseline +
runner) is specified to run agent SQL through `/api/internal/query`, but that
endpoint is not built until step 4. Rather than reorder the plan and give up having
real numbers early, the baseline runs on `DirectExecutor` and is switched to
`InternalApiExecutor` the moment step 4 lands. The swap is one constructor argument
and changes nothing else -- and because the baseline's numbers are the control arm,
re-running it after the swap also serves as a check that the two paths agree.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

from .envelope import ExecResult

DEFAULT_TIMEOUT_MS = 5000
DEFAULT_MAX_ROWS = 1000


@dataclass(frozen=True)
class ExecOutcome:
    """Mirrors execute-query.ts's ExecuteResult so both paths report identically."""

    status: str  # "ok" | "timeout" | "error" | "validation_rejected"
    result: ExecResult | None = None
    error: str | None = None
    duration_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class SqlExecutor(Protocol):
    name: str

    def execute(self, sql: str) -> ExecOutcome: ...


class DirectExecutor:
    """Read-only `sandbox_ro` connection, mirroring execute-query.ts's guarantees.

    Deliberately reproduces the same three protections rather than trusting the
    role alone: BEGIN READ ONLY, a statement timeout, and a row cap. `sandbox_ro`
    is the security boundary and would reject a mutation regardless, but a runaway
    sequential scan against the live 216MB dataset is a cost problem, not a
    security one, and the role does not bound that.
    """

    name = "direct-sandbox-ro"

    def __init__(
        self,
        dsn: str | None = None,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> None:
        self._dsn = dsn or os.environ.get("SANDBOX_RO_DATABASE_URL")
        if not self._dsn:
            raise RuntimeError(
                "SANDBOX_RO_DATABASE_URL is not set. Eval runs need the live "
                "2023-24 dataset; the gold queries return nothing without it."
            )
        if not isinstance(timeout_ms, int) or timeout_ms <= 0:
            raise ValueError(f"timeout_ms must be a positive integer, got {timeout_ms!r}")
        if not isinstance(max_rows, int) or max_rows <= 0:
            raise ValueError(f"max_rows must be a positive integer, got {max_rows!r}")
        self._timeout_ms = timeout_ms
        self._max_rows = max_rows
        self._conn = None

    def _connect(self):
        import psycopg2

        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self._dsn)
        return self._conn

    def execute(self, sql: str) -> ExecOutcome:
        import time

        import psycopg2

        started = time.monotonic()
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("BEGIN READ ONLY")
                # No bind parameter is possible for SET LOCAL; the value is ours,
                # never the model's, and was shape-checked in __init__.
                cur.execute(f"SET LOCAL statement_timeout = {self._timeout_ms}")
                cur.execute(sql)
                rows = cur.fetchmany(self._max_rows + 1)
                columns = tuple(desc[0] for desc in cur.description or ())
                truncated = len(rows) > self._max_rows
                if truncated:
                    rows = rows[: self._max_rows]
            conn.rollback()
            return ExecOutcome(
                status="ok",
                result=ExecResult(
                    columns=columns,
                    rows=tuple(tuple(row) for row in rows),
                    truncated=truncated,
                ),
                duration_ms=(time.monotonic() - started) * 1000,
            )
        except psycopg2.errors.QueryCanceled as err:
            conn.rollback()
            return ExecOutcome(
                status="timeout",
                error=str(err).strip(),
                duration_ms=(time.monotonic() - started) * 1000,
            )
        except psycopg2.Error as err:
            conn.rollback()
            return ExecOutcome(
                status="error",
                error=str(err).strip(),
                duration_ms=(time.monotonic() - started) * 1000,
            )

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None


class InternalApiExecutor:
    """POSTs to /api/internal/query with the eval service token.

    Lands in `app.query_log` with `source='eval'`, so eval traffic stays separable
    from real user traffic in the audit trail (migration 0004).
    """

    name = "internal-api"

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        user_id: int | None = None,
        *,
        timeout_s: float = 30.0,
    ) -> None:
        self._base_url = (base_url or os.environ.get("EVAL_API_BASE_URL", "")).rstrip("/")
        self._token = token or os.environ.get("EVAL_SERVICE_TOKEN", "")
        raw_user = user_id if user_id is not None else os.environ.get("EVAL_USER_ID")
        if not self._base_url or not self._token or raw_user in (None, ""):
            raise RuntimeError(
                "InternalApiExecutor needs EVAL_API_BASE_URL, EVAL_SERVICE_TOKEN, "
                "and EVAL_USER_ID."
            )
        self._user_id = int(raw_user)
        self._timeout_s = timeout_s

    def execute(self, sql: str) -> ExecOutcome:
        import time

        import httpx

        started = time.monotonic()
        try:
            response = httpx.post(
                f"{self._base_url}/api/internal/query",
                # No `source` field: the endpoint derives it from which token
                # authenticated, so a caller cannot label its own traffic. Sending
                # one here would be ignored server-side and misleading here.
                json={"sql": sql, "userId": self._user_id},
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=self._timeout_s,
            )
        except httpx.HTTPError as err:
            # Transport failure is harness breakage, not an agent miss -- surfacing
            # it as an "error" outcome would quietly depress the valid-SQL rate.
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
        if response.status_code in (401, 403):
            raise RuntimeError(
                f"/api/internal/query rejected the eval token ({response.status_code}). "
                "This is a configuration failure, not an agent failure."
            )
        if response.status_code == 429:
            raise RuntimeError(
                "/api/internal/query rate-limited the eval user. Raise the eval "
                "user's limit or pace the runner (see .agents/p4_agent.md)."
            )

        status = {504: "timeout"}.get(response.status_code, "error")
        if response.status_code == 400 and "durationMs" not in body:
            status = "validation_rejected"
        return ExecOutcome(
            status=status,
            error=body.get("error", f"HTTP {response.status_code}"),
            duration_ms=body.get("durationMs", elapsed_ms),
        )
