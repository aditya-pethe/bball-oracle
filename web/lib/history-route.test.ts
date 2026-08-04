import { afterAll, beforeAll, beforeEach, describe, expect, inject, it, vi } from "vitest";
import { Pool } from "pg";

vi.mock("./require-session", () => ({ requireSession: vi.fn() }));

import { requireSession } from "./require-session";
import { closePools } from "./execute-query";
import { GET } from "../app/api/history/route";

const mockSession = vi.mocked(requireSession);

let owner: Pool;
let userA: number;
let userB: number;

async function insertLog(
  userId: number,
  overrides: Partial<{
    queryText: string;
    status: string;
    startedAt: string;
    durationMs: number | null;
    rowCount: number | null;
    truncated: boolean;
  }> = {},
) {
  const {
    queryText = "SELECT 1",
    status = "ok",
    startedAt = new Date().toISOString(),
    durationMs = 12,
    rowCount = 1,
    truncated = false,
  } = overrides;
  await owner.query(
    `INSERT INTO app.query_log (user_id, query_text, status, started_at, duration_ms, row_count, truncated)
     VALUES ($1, $2, $3, $4, $5, $6, $7)`,
    [userId, queryText, status, startedAt, durationMs, rowCount, truncated],
  );
}

beforeAll(async () => {
  process.env.SANDBOX_RO_DATABASE_URL = inject("sandboxUrl");
  process.env.APP_RW_DATABASE_URL = inject("appUrl");
  owner = new Pool({ connectionString: inject("ownerUrl"), max: 2 });
  const { rows } = await owner.query(
    `INSERT INTO app.users (name) VALUES ('history-route-test-a'), ('history-route-test-b')
     RETURNING id`,
  );
  userA = rows[0].id;
  userB = rows[1].id;
});

afterAll(async () => {
  await owner.end();
  await closePools();
});

beforeEach(() => {
  mockSession.mockResolvedValue({ userId: userA });
});

describe("auth gate", () => {
  it("returns 401 with no session", async () => {
    mockSession.mockResolvedValue(null);
    const res = await GET();
    expect(res.status).toBe(401);
    expect((await res.json()).error).toBe("authentication required");
  });
});

describe("isolation", () => {
  it("returns only the signed-in user's own rows, never another user's", async () => {
    await insertLog(userA, { queryText: "SELECT 'a-only'" });
    await insertLog(userB, { queryText: "SELECT 'b-only'" });

    mockSession.mockResolvedValue({ userId: userA });
    const res = await GET();
    const body = await res.json();

    const sqlTexts = body.queries.map((q: { sql: string }) => q.sql);
    expect(sqlTexts).toContain("SELECT 'a-only'");
    expect(sqlTexts).not.toContain("SELECT 'b-only'");
  });
});

describe("shape and ordering", () => {
  it("returns the documented fields with correct types", async () => {
    await insertLog(userA, {
      queryText: "SELECT 'shape-check'",
      status: "ok",
      durationMs: 42,
      rowCount: 7,
      truncated: true,
    });

    const res = await GET();
    const body = await res.json();
    const row = body.queries.find((q: { sql: string }) => q.sql === "SELECT 'shape-check'");

    expect(row).toBeTruthy();
    expect(row.status).toBe("ok");
    expect(typeof row.startedAt).toBe("string");
    expect(new Date(row.startedAt).toString()).not.toBe("Invalid Date");
    expect(row.durationMs).toBe(42);
    expect(row.rowCount).toBe(7);
    expect(row.truncated).toBe(true);
  });

  it("returns validation_rejected rows with null duration and row count", async () => {
    await insertLog(userA, {
      queryText: "DROP TABLE nba.pbp_event",
      status: "validation_rejected",
      durationMs: null,
      rowCount: null,
      truncated: false,
    });

    const res = await GET();
    const body = await res.json();
    const row = body.queries.find(
      (q: { sql: string }) => q.sql === "DROP TABLE nba.pbp_event",
    );

    expect(row.status).toBe("validation_rejected");
    expect(row.durationMs).toBeNull();
    expect(row.rowCount).toBeNull();
  });

  it("orders newest-first", async () => {
    const { rows } = await owner.query(
      "INSERT INTO app.users (name) VALUES ('history-route-test-order') RETURNING id",
    );
    const orderUser = rows[0].id;

    await insertLog(orderUser, { queryText: "SELECT 'first'", startedAt: "2024-01-01T00:00:00Z" });
    await insertLog(orderUser, { queryText: "SELECT 'second'", startedAt: "2024-01-02T00:00:00Z" });
    await insertLog(orderUser, { queryText: "SELECT 'third'", startedAt: "2024-01-03T00:00:00Z" });

    mockSession.mockResolvedValue({ userId: orderUser });
    const res = await GET();
    const body = await res.json();

    expect(body.queries.map((q: { sql: string }) => q.sql)).toEqual([
      "SELECT 'third'",
      "SELECT 'second'",
      "SELECT 'first'",
    ]);
  });

  it("caps at 50 rows, keeping the most recent", async () => {
    const { rows } = await owner.query(
      "INSERT INTO app.users (name) VALUES ('history-route-test-limit') RETURNING id",
    );
    const limitUser = rows[0].id;

    const base = new Date("2024-06-01T00:00:00Z").getTime();
    for (let i = 0; i < 55; i++) {
      await insertLog(limitUser, {
        queryText: `SELECT ${i}`,
        startedAt: new Date(base + i * 1000).toISOString(),
      });
    }

    mockSession.mockResolvedValue({ userId: limitUser });
    const res = await GET();
    const body = await res.json();

    expect(body.queries).toHaveLength(50);
    expect(body.queries[0].sql).toBe("SELECT 54");
    expect(body.queries[49].sql).toBe("SELECT 5");
  });
});
