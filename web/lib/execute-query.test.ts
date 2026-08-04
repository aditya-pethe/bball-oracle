import { Pool } from "pg";
import { afterAll, beforeAll, describe, expect, inject, it } from "vitest";

import { closePools, executeQuery, getAppPool, getSandboxPool, logQuery } from "./execute-query";
import { FIXTURE_ROWS, FIXTURE_USER_ID } from "./test-cluster";

const CTX = { userId: FIXTURE_USER_ID, clientIp: "203.0.113.7" };

let owner: Pool;

type LogRow = {
  user_id: number | null;
  client_ip: string | null;
  query_text: string;
  status: string;
  duration_ms: number | null;
  row_count: number | null;
  truncated: boolean;
  error_text: string | null;
};

async function countLogs(): Promise<number> {
  const { rows } = await owner.query<{ n: string }>("SELECT count(*)::text AS n FROM app.query_log");
  return Number(rows[0].n);
}

async function latestLog(): Promise<LogRow> {
  const { rows } = await owner.query<LogRow>(
    "SELECT * FROM app.query_log ORDER BY id DESC LIMIT 1",
  );
  return rows[0];
}

/** Runs `fn` and asserts it added exactly one query_log row, returning that row. */
async function withLogCount<T>(fn: () => Promise<T>): Promise<{ result: T; log: LogRow }> {
  const before = await countLogs();
  const result = await fn();
  expect(await countLogs()).toBe(before + 1);
  return { result, log: await latestLog() };
}

beforeAll(async () => {
  process.env.SANDBOX_RO_DATABASE_URL = inject("sandboxUrl");
  process.env.APP_RW_DATABASE_URL = inject("appUrl");
  owner = new Pool({ connectionString: inject("ownerUrl"), max: 2 });
});

afterAll(async () => {
  await closePools();
  await owner.end();
});

describe("executeQuery", () => {
  it("runs user SQL as sandbox_ro", async () => {
    const result = await executeQuery("SELECT current_user", CTX);

    expect(result.status).toBe("ok");
    if (result.status !== "ok") return;
    expect(result.rows).toEqual([["sandbox_ro"]]);
    expect(result.columns).toEqual(["current_user"]);
  });

  it("returns fixture rows and logs an ok row", async () => {
    const { result, log } = await withLogCount(() =>
      executeQuery("SELECT eventnum, player1_name FROM nba.pbp_event ORDER BY eventnum", CTX),
    );

    expect(result.status).toBe("ok");
    if (result.status !== "ok") return;
    expect(result.rowCount).toBe(FIXTURE_ROWS);
    expect(result.rows).toHaveLength(FIXTURE_ROWS);
    expect(result.rows[0]).toEqual([1, "Player 1"]);
    expect(result.columns).toEqual(["eventnum", "player1_name"]);
    expect(result.truncated).toBe(false);

    expect(log.status).toBe("ok");
    expect(log.row_count).toBe(FIXTURE_ROWS);
    expect(log.truncated).toBe(false);
    expect(log.duration_ms).not.toBeNull();
    expect(log.user_id).toBe(FIXTURE_USER_ID);
    expect(log.client_ip).toBe(CTX.clientIp);
    expect(log.error_text).toBeNull();
  });

  it("classifies our statement timeout as timeout, not error", async () => {
    const { result, log } = await withLogCount(() =>
      executeQuery("SELECT pg_sleep(2)", CTX, { timeoutMs: 200 }),
    );

    expect(result.status).toBe("timeout");
    expect(result.durationMs).toBeLessThan(2000);
    expect(log.status).toBe("timeout");
    expect(log.row_count).toBeNull();
  });

  it("caps rows server-side and reports truncation", async () => {
    const { result, log } = await withLogCount(() =>
      executeQuery("SELECT eventnum FROM nba.pbp_event ORDER BY eventnum", CTX, { maxRows: 5 }),
    );

    expect(result.status).toBe("ok");
    if (result.status !== "ok") return;
    expect(result.rows).toHaveLength(5);
    expect(result.rowCount).toBe(5);
    expect(result.truncated).toBe(true);
    expect(result.rows.at(-1)).toEqual([5]);
    expect(log.truncated).toBe(true);
    expect(log.row_count).toBe(5);
  });

  it("does not report truncation when the result fits under the cap", async () => {
    const { result, log } = await withLogCount(() =>
      executeQuery("SELECT eventnum FROM nba.pbp_event WHERE eventnum <= 3", CTX, { maxRows: 5 }),
    );

    expect(result.status).toBe("ok");
    if (result.status !== "ok") return;
    expect(result.rows).toHaveLength(3);
    expect(result.truncated).toBe(false);
    expect(log.truncated).toBe(false);
  });

  it("reports a result that exactly fills the cap as untruncated", async () => {
    const result = await executeQuery(
      "SELECT eventnum FROM nba.pbp_event WHERE eventnum <= 5 ORDER BY eventnum",
      CTX,
      { maxRows: 5 },
    );

    expect(result.status).toBe("ok");
    if (result.status !== "ok") return;
    expect(result.rows).toHaveLength(5);
    expect(result.truncated).toBe(false);
  });

  it("caps rows in the server, not by draining the result client-side", async () => {
    // Target-list form on purpose: a set-returning function in FROM is materialized by the
    // planner, which would time out here no matter how few rows the client asks for.
    const result = await executeQuery("SELECT generate_series(1, 50000000) AS n", CTX, {
      maxRows: 5,
      timeoutMs: 3000,
    });

    expect(result.status).toBe("ok");
    if (result.status !== "ok") return;
    expect(result.rows).toHaveLength(5);
    expect(result.truncated).toBe(true);
  });

  it("leaves no sandbox connection stuck in an open transaction", async () => {
    await executeQuery("SELECT eventnum FROM nba.pbp_event", CTX, { maxRows: 2 });
    await executeQuery("SELECT * FROM nba.nonexistent", CTX);
    await executeQuery("SELECT pg_sleep(2)", CTX, { timeoutMs: 150 });

    const { rows } = await owner.query<{ n: string }>(
      `SELECT count(*)::text AS n FROM pg_stat_activity
       WHERE usename = 'sandbox_ro' AND state LIKE 'idle in transaction%'`,
    );
    expect(Number(rows[0].n)).toBe(0);
  });

  it("returns the Postgres message for a bad query and logs an error row", async () => {
    const { result, log } = await withLogCount(() =>
      executeQuery("SELECT * FROM nba.nonexistent", CTX),
    );

    expect(result.status).toBe("error");
    if (result.status !== "error") return;
    expect(result.message).toMatch(/nonexistent/);
    expect(log.status).toBe("error");
    expect(log.error_text).toMatch(/nonexistent/);
    expect(log.row_count).toBeNull();
  });

  it.each([
    ["DELETE", "DELETE FROM nba.pbp_event"],
    [
      "INSERT",
      `INSERT INTO nba.pbp_event (season, season_type, game_id, eventnum, eventmsgtype, period)
       VALUES (2023, 'regular', 99, 99, 1, 1)`,
    ],
    ["DROP", "DROP TABLE nba.pbp_event"],
  ])("blocks %s at the database, not in this module", async (_label, sql) => {
    const { result, log } = await withLogCount(() => executeQuery(sql, CTX));

    expect(result.status).toBe("error");
    expect(log.status).toBe("error");
    expect(log.error_text).not.toBeNull();

    const { rows } = await owner.query<{ n: string }>(
      "SELECT count(*)::text AS n FROM nba.pbp_event",
    );
    expect(Number(rows[0].n)).toBe(FIXTURE_ROWS);
  });

  it("rejects multi-statement input", async () => {
    const { result, log } = await withLogCount(() => executeQuery("SELECT 1; SELECT 2", CTX));

    expect(result.status).toBe("error");
    expect(log.status).toBe("error");
    expect(log.error_text).not.toBeNull();
  });

  it("preserves duplicate column names", async () => {
    const result = await executeQuery("SELECT 1 AS a, 2 AS a", CTX);

    expect(result.status).toBe("ok");
    if (result.status !== "ok") return;
    expect(result.columns).toEqual(["a", "a"]);
    expect(result.rows).toEqual([[1, 2]]);
  });

  it("writes exactly one log row per call regardless of outcome", async () => {
    const before = await countLogs();

    await executeQuery("SELECT 1", CTX);
    await executeQuery("SELECT pg_sleep(2)", CTX, { timeoutMs: 150 });
    await executeQuery("SELECT * FROM nba.nonexistent", CTX);
    await executeQuery("SELECT eventnum FROM nba.pbp_event", CTX, { maxRows: 2 });

    expect(await countLogs()).toBe(before + 4);
  });

  it("fails the call when the query cannot be audited", async () => {
    await owner.query("REVOKE INSERT ON app.query_log FROM app_rw");
    try {
      await expect(executeQuery("SELECT 1", CTX)).rejects.toThrow(/permission denied/i);
    } finally {
      await owner.query("GRANT INSERT ON app.query_log TO app_rw");
    }

    const { result } = await withLogCount(() => executeQuery("SELECT 1", CTX));
    expect(result.status).toBe("ok");
  });

  it("leaves the sandbox pool usable after failures", async () => {
    for (let i = 0; i < 8; i++) {
      await executeQuery("SELECT * FROM nba.nonexistent", CTX);
      await executeQuery("SELECT pg_sleep(1)", CTX, { timeoutMs: 100 });
      await executeQuery("SELECT eventnum FROM nba.pbp_event", CTX, { maxRows: 2 });
    }

    const result = await executeQuery("SELECT current_user", CTX);
    expect(result.status).toBe("ok");
  });
});

describe("logQuery", () => {
  it("records a validation_rejected attempt with no duration or row count", async () => {
    const before = await countLogs();

    await logQuery({
      userId: FIXTURE_USER_ID,
      clientIp: null,
      queryText: "DROP TABLE nba.pbp_event",
      status: "validation_rejected",
      errorText: "only SELECT statements are allowed",
    });

    expect(await countLogs()).toBe(before + 1);
    const log = await latestLog();
    expect(log.status).toBe("validation_rejected");
    expect(log.duration_ms).toBeNull();
    expect(log.row_count).toBeNull();
    expect(log.truncated).toBe(false);
    expect(log.client_ip).toBeNull();
    expect(log.error_text).toBe("only SELECT statements are allowed");
  });
});

describe("pools", () => {
  it("exposes the sandbox and app pools as distinct reusable handles", () => {
    expect(getSandboxPool()).toBe(getSandboxPool());
    expect(getAppPool()).toBe(getAppPool());
    expect(getSandboxPool()).not.toBe(getAppPool());
  });
});
