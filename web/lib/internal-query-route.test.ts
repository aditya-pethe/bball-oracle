/**
 * The second door's enforcement tests. `/api/internal/query` is the highest-risk surface in
 * Phase 4: it executes user SQL without a browser session. Everything the session-gated route
 * guarantees has to hold here too, and the token must be the only way in.
 *
 * These are the enforcement of AGENTS.md's critical rules on this path, not a formality.
 */
import {
  afterAll,
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  inject,
  it,
  vi,
} from "vitest";
import { Pool } from "pg";

vi.mock("./require-session", () => ({ requireSession: vi.fn() }));

import { requireSession } from "./require-session";
import { closePools } from "./execute-query";
import { POST } from "../app/api/internal/query/route";
import { POST as editorPost } from "../app/api/query/route";

const mockSession = vi.mocked(requireSession);

const AGENT_TOKEN = "agent-token-for-tests-0123456789abcdef";
const EVAL_TOKEN = "eval-token-for-tests-0123456789abcdef";

let owner: Pool;
let userId: number;

beforeAll(async () => {
  process.env.SANDBOX_RO_DATABASE_URL = inject("sandboxUrl");
  process.env.APP_RW_DATABASE_URL = inject("appUrl");
  owner = new Pool({ connectionString: inject("ownerUrl"), max: 2 });
  const { rows } = await owner.query(
    "INSERT INTO app.users (name) VALUES ('internal-route-test') RETURNING id",
  );
  userId = rows[0].id;
});

afterAll(async () => {
  await owner.end();
  await closePools();
});

beforeEach(() => {
  process.env.AGENT_SERVICE_TOKEN = AGENT_TOKEN;
  process.env.EVAL_SERVICE_TOKEN = EVAL_TOKEN;
});

afterEach(() => {
  delete process.env.AGENT_SERVICE_TOKEN;
  delete process.env.EVAL_SERVICE_TOKEN;
});

function post(body: unknown, headers: Record<string, string> = {}) {
  return POST(
    new Request("http://localhost/api/internal/query", {
      method: "POST",
      body: typeof body === "string" ? body : JSON.stringify(body),
      headers: { "content-type": "application/json", ...headers },
    }),
  );
}

function asAgent(body: unknown) {
  return post(body, { authorization: `Bearer ${AGENT_TOKEN}` });
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

describe("service token gate", () => {
  it("rejects a request with no Authorization header, without logging", async () => {
    const before = await countLogs();
    const res = await post({ sql: "SELECT 1", userId });
    expect(res.status).toBe(401);
    expect(await countLogs()).toBe(before);
  });

  it.each([
    ["wrong token", `Bearer definitely-not-the-token-0123456789ab`],
    ["truncated token", `Bearer ${AGENT_TOKEN.slice(0, -1)}`],
    ["token with trailing junk", `Bearer ${AGENT_TOKEN}x`],
    ["wrong scheme", `Basic ${AGENT_TOKEN}`],
    ["bare token, no scheme", AGENT_TOKEN],
    ["empty bearer", "Bearer "],
    ["malformed header", "not-a-header"],
  ])("rejects %s", async (_label, authorization) => {
    const before = await countLogs();
    const res = await post({ sql: "SELECT 1", userId }, { authorization });
    expect(res.status).toBe(401);
    expect(await countLogs()).toBe(before);
  });

  it("rejects everything when the token env vars are unset", async () => {
    delete process.env.AGENT_SERVICE_TOKEN;
    delete process.env.EVAL_SERVICE_TOKEN;
    const res = await post({ sql: "SELECT 1", userId }, { authorization: "Bearer " });
    expect(res.status).toBe(401);
  });

  it("does not accept an empty token when the env var is set to empty", async () => {
    process.env.AGENT_SERVICE_TOKEN = "";
    const res = await post({ sql: "SELECT 1", userId }, { authorization: "Bearer x" });
    expect(res.status).toBe(401);
  });

  it("rejects before touching the body", async () => {
    const res = await post("not json");
    expect(res.status).toBe(401);
  });
});

describe("user attribution", () => {
  it.each([
    ["missing", undefined],
    ["null", null],
    ["non-numeric string", "not-a-number"],
    ["float", 1.5],
    ["zero", 0],
    ["negative", -3],
    ["object", { id: 1 }],
    ["array", [1]],
    ["boolean", true],
  ])("rejects a %s userId with a valid token, without logging", async (_label, value) => {
    const before = await countLogs();
    const res = await post(
      { sql: "SELECT 1", userId: value },
      { authorization: `Bearer ${AGENT_TOKEN}` },
    );
    expect(res.status).toBe(400);
    expect((await res.json()).error).toMatch(/userId/);
    expect(await countLogs()).toBe(before);
  });

  it("rejects a nonexistent userId", async () => {
    const res = await asAgent({ sql: "SELECT 1", userId: 987654321 });
    expect(res.status).toBe(400);
    expect((await res.json()).error).toMatch(/unknown userId/);
  });

  it("accepts a numeric-string userId", async () => {
    const res = await asAgent({ sql: "SELECT 1", userId: String(userId) });
    expect(res.status).toBe(200);
    expect((await lastLog()).user_id).toBe(userId);
  });

  it("rejects a missing sql field", async () => {
    const res = await asAgent({ userId });
    expect(res.status).toBe(400);
  });
});

describe("provenance is the endpoint's to decide", () => {
  it("logs source=agent for the agent token", async () => {
    const res = await asAgent({ sql: "SELECT 1", userId });
    expect(res.status).toBe(200);
    expect((await lastLog()).source).toBe("agent");
  });

  it("logs source=eval for the eval token", async () => {
    const res = await post(
      { sql: "SELECT 1", userId },
      { authorization: `Bearer ${EVAL_TOKEN}` },
    );
    expect(res.status).toBe(200);
    expect((await lastLog()).source).toBe("eval");
  });

  it.each(["editor", "eval", "nonsense"])(
    "ignores a body-supplied source=%s and uses the token's",
    async (spoofed) => {
      const res = await asAgent({ sql: "SELECT 1", userId, source: spoofed });
      expect(res.status).toBe(200);
      expect((await lastLog()).source).toBe("agent");
    },
  );

  it("records source on a rejected attempt too, so the audit trail has no gaps", async () => {
    const res = await asAgent({ sql: "DROP TABLE nba.pbp_event", userId });
    expect(res.status).toBe(400);
    const log = await lastLog();
    expect(log.status).toBe("validation_rejected");
    expect(log.source).toBe("agent");
  });

  it("links an attempt to its conversation message", async () => {
    const { rows: conv } = await owner.query(
      "INSERT INTO app.conversation (user_id, title) VALUES ($1, 'test') RETURNING id",
      [userId],
    );
    const { rows: msg } = await owner.query(
      `INSERT INTO app.conversation_message (conversation_id, role, content)
       VALUES ($1, 'assistant', '{"outcome":"answer"}') RETURNING id`,
      [conv[0].id],
    );
    const res = await asAgent({
      sql: "SELECT 1",
      userId,
      conversationMessageId: msg[0].id,
    });
    expect(res.status).toBe(200);
    expect(Number((await lastLog()).conversation_message_id)).toBe(Number(msg[0].id));
  });

  it("labels the session-gated door 'editor', not whatever its body claims", async () => {
    mockSession.mockResolvedValue({ userId });
    const res = await editorPost(
      new Request("http://localhost/api/query", {
        method: "POST",
        body: JSON.stringify({ sql: "SELECT 1", source: "eval" }),
        headers: { "content-type": "application/json" },
      }),
    );
    expect(res.status).toBe(200);
    expect((await lastLog()).source).toBe("editor");
  });
});

describe("the chain is not weakened by the second door", () => {
  it("runs as sandbox_ro", async () => {
    const res = await asAgent({ sql: "SELECT current_user", userId });
    expect(res.status).toBe(200);
    expect((await res.json()).rows).toEqual([["sandbox_ro"]]);
  });

  it("still enforces the statement timeout", async () => {
    const before = await countLogs();
    const res = await asAgent({ sql: "SELECT pg_sleep(30)", userId });
    expect(res.status).toBe(504);
    const body = await res.json();
    expect(body.error).toMatch(/time limit/);
    expect(body.durationMs).toBeLessThan(30_000);
    expect(await countLogs()).toBe(before + 1);
    expect((await lastLog()).status).toBe("timeout");
  });

  it("still enforces the row cap", async () => {
    const res = await asAgent({ sql: "SELECT g FROM generate_series(1, 1500) AS g", userId });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.rowCount).toBe(1000);
    expect(body.truncated).toBe(true);
    const log = await lastLog();
    expect(log.truncated).toBe(true);
    expect(log.row_count).toBe(1000);
  });

  it.each([
    ["success", "SELECT 1", 200, "ok"],
    ["validation rejection", "DELETE FROM nba.pbp_event", 400, "validation_rejected"],
    ["SQL error", "SELECT * FROM nba.nonexistent", 400, "error"],
    ["timeout", "SELECT pg_sleep(30)", 504, "timeout"],
  ])(
    "writes exactly one query_log row for a %s",
    async (_label, sql, status, logStatus) => {
      const before = await countLogs();
      const res = await asAgent({ sql, userId });
      expect(res.status).toBe(status);
      expect(await countLogs()).toBe(before + 1);
      const log = await lastLog();
      expect(log.status).toBe(logStatus);
      expect(log.query_text).toBe(sql);
      expect(log.source).toBe("agent");
    },
  );

  it.each([
    "DELETE FROM nba.pbp_event",
    "INSERT INTO nba.pbp_event (game_id) VALUES (1)",
    "UPDATE nba.pbp_event SET period = 4",
    "DROP TABLE nba.pbp_event",
    "SELECT 1; SELECT 2",
    "WITH x AS (DELETE FROM nba.pbp_event RETURNING *) SELECT * FROM x",
    "SELECT * FROM nba.pbp_event FOR UPDATE",
    "SELECT * FROM app.query_log",
  ])("rejects the mutation %s and logs it", async (sql) => {
    const before = await countLogs();
    const res = await asAgent({ sql, userId });
    expect(res.status).toBe(400);
    expect(await countLogs()).toBe(before + 1);
  });

  it("is stopped by sandbox_ro's grants even with the validator bypassed", async () => {
    // The validator is defense in depth, not the boundary (AGENTS.md). Prove the claim by
    // going straight at the executor -- the same connection the route uses, no validation.
    const { executeQuery } = await import("./execute-query");
    const { rows: fixtureBefore } = await owner.query<{ n: string }>(
      "SELECT count(*)::text AS n FROM nba.pbp_event",
    );

    for (const sql of [
      "DELETE FROM nba.pbp_event",
      "UPDATE nba.pbp_event SET period = 4",
      "DROP TABLE nba.pbp_event",
      "INSERT INTO app.query_log (query_text, status, source) VALUES ('x', 'ok', 'editor')",
    ]) {
      const result = await executeQuery(sql, { userId, clientIp: null, source: "agent" });
      expect(result.status).toBe("error");
    }

    const { rows: fixtureAfter } = await owner.query<{ n: string }>(
      "SELECT count(*)::text AS n FROM nba.pbp_event",
    );
    expect(fixtureAfter[0].n).toBe(fixtureBefore[0].n);
  });

  it("rate-limits an attributed service caller like any other user", async () => {
    const { rows } = await owner.query(
      "INSERT INTO app.users (name) VALUES ('internal-burst') RETURNING id",
    );
    const burstUser = rows[0].id;
    await owner.query(
      `INSERT INTO app.query_log (user_id, query_text, status, source)
       SELECT $1, 'SELECT 1', 'ok', 'agent' FROM generate_series(1, 30)`,
      [burstUser],
    );

    const res = await asAgent({ sql: "SELECT 1", userId: burstUser });
    expect(res.status).toBe(429);
    expect(Number(res.headers.get("retry-after"))).toBeGreaterThan(0);

    const { rows: after } = await owner.query(
      "SELECT count(*)::int AS n FROM app.query_log WHERE user_id = $1",
      [burstUser],
    );
    expect(after[0].n).toBe(30);
  });
});
