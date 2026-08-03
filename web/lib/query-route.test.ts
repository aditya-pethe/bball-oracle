import { afterAll, beforeAll, beforeEach, describe, expect, inject, it, vi } from "vitest";
import { Pool } from "pg";

vi.mock("./require-session", () => ({ requireSession: vi.fn() }));

import { requireSession } from "./require-session";
import { closePools } from "./execute-query";
import { POST } from "../app/api/query/route";

const mockSession = vi.mocked(requireSession);

let owner: Pool;
let userId: number;

beforeAll(async () => {
  process.env.SANDBOX_RO_DATABASE_URL = inject("sandboxUrl");
  process.env.APP_RW_DATABASE_URL = inject("appUrl");
  owner = new Pool({ connectionString: inject("ownerUrl"), max: 2 });
  const { rows } = await owner.query(
    "INSERT INTO app.users (name) VALUES ('route-test') RETURNING id",
  );
  userId = rows[0].id;
});

afterAll(async () => {
  await owner.end();
  await closePools();
});

beforeEach(() => {
  mockSession.mockResolvedValue({ userId });
});

function post(body: unknown, headers: Record<string, string> = {}) {
  return POST(
    new Request("http://localhost/api/query", {
      method: "POST",
      body: typeof body === "string" ? body : JSON.stringify(body),
      headers: { "content-type": "application/json", ...headers },
    }),
  );
}

async function countLogs(): Promise<number> {
  const { rows } = await owner.query(
    "SELECT count(*)::int AS n FROM app.query_log WHERE user_id = $1",
    [userId],
  );
  return rows[0].n;
}

async function lastLog() {
  const { rows } = await owner.query(
    "SELECT * FROM app.query_log WHERE user_id = $1 ORDER BY id DESC LIMIT 1",
    [userId],
  );
  return rows[0];
}

describe("auth gate", () => {
  it("returns 401 with no session and writes no log row", async () => {
    mockSession.mockResolvedValue(null);
    const before = await countLogs();
    const res = await post({ sql: "SELECT 1" });
    expect(res.status).toBe(401);
    expect(await countLogs()).toBe(before);
  });
});

describe("request body", () => {
  it("rejects non-JSON bodies without logging", async () => {
    const before = await countLogs();
    const res = await post("not json");
    expect(res.status).toBe(400);
    expect(await countLogs()).toBe(before);
  });

  it("rejects a missing sql field", async () => {
    const res = await post({ query: "SELECT 1" });
    expect(res.status).toBe(400);
  });

  it("rejects a blank sql field", async () => {
    const res = await post({ sql: "   " });
    expect(res.status).toBe(400);
  });
});

describe("execution", () => {
  it("runs as sandbox_ro", async () => {
    const res = await post({ sql: "SELECT current_user" });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.rows).toEqual([["sandbox_ro"]]);
    expect(body.truncated).toBe(false);
  });

  it("returns nba rows with columns and logs ok", async () => {
    const before = await countLogs();
    const res = await post({ sql: "SELECT game_id, eventnum FROM nba.pbp_event ORDER BY eventnum" });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.columns).toEqual(["game_id", "eventnum"]);
    expect(body.rowCount).toBeGreaterThan(0);
    expect(await countLogs()).toBe(before + 1);
    expect((await lastLog()).status).toBe("ok");
  });

  it("truncates at the row cap with a flag", async () => {
    const res = await post({ sql: "SELECT g FROM generate_series(1, 1500) AS g" });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.rowCount).toBe(1000);
    expect(body.truncated).toBe(true);
    expect((await lastLog()).truncated).toBe(true);
  });

  it("surfaces SQL errors as 400 and logs error", async () => {
    const before = await countLogs();
    const res = await post({ sql: "SELECT * FROM nba.nonexistent" });
    expect(res.status).toBe(400);
    expect((await res.json()).error).toMatch(/nonexistent/);
    expect(await countLogs()).toBe(before + 1);
    expect((await lastLog()).status).toBe("error");
  });
});

describe("validation layer", () => {
  it.each([
    "DELETE FROM nba.pbp_event",
    "INSERT INTO nba.pbp_event (game_id) VALUES (1)",
    "DROP TABLE nba.pbp_event",
    "SELECT 1; SELECT 2",
    "WITH x AS (DELETE FROM nba.pbp_event RETURNING *) SELECT * FROM x",
    "SELECT * FROM nba.pbp_event FOR UPDATE",
  ])("rejects %s with 400 and logs validation_rejected", async (sql) => {
    const before = await countLogs();
    const res = await post({ sql });
    expect(res.status).toBe(400);
    expect((await res.json()).error).toBeTruthy();
    expect(await countLogs()).toBe(before + 1);
    const log = await lastLog();
    expect(log.status).toBe("validation_rejected");
    expect(log.query_text).toBe(sql);
    expect(log.duration_ms).toBeNull();
    expect(log.row_count).toBeNull();
  });
});

describe("rate limiting", () => {
  it("returns 429 with Retry-After once the per-user window is full, without logging", async () => {
    const { rows } = await owner.query(
      "INSERT INTO app.users (name) VALUES ('route-test-burst') RETURNING id",
    );
    const burstUser = rows[0].id;
    mockSession.mockResolvedValue({ userId: burstUser });
    await owner.query(
      `INSERT INTO app.query_log (user_id, query_text, status)
       SELECT $1, 'SELECT 1', 'ok' FROM generate_series(1, 30)`,
      [burstUser],
    );

    const res = await post({ sql: "SELECT 1" });
    expect(res.status).toBe(429);
    expect(Number(res.headers.get("retry-after"))).toBeGreaterThan(0);
    const { rows: after } = await owner.query(
      "SELECT count(*)::int AS n FROM app.query_log WHERE user_id = $1",
      [burstUser],
    );
    expect(after[0].n).toBe(30);
  });

  it("returns 429 on the per-IP window across users", async () => {
    const ip = "198.51.100.77";
    await owner.query(
      `INSERT INTO app.query_log (user_id, client_ip, query_text, status)
       SELECT NULL, $1::inet, 'SELECT 1', 'ok' FROM generate_series(1, 60)`,
      [ip],
    );
    const res = await post({ sql: "SELECT 1" }, { "x-forwarded-for": `${ip}, 10.0.0.1` });
    expect(res.status).toBe(429);
  });
});
