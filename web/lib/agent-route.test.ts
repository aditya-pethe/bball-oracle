/**
 * `/api/agent`: the proxy behavior (session gate, userId substitution, the service token
 * forwarded but never echoed back, clean errors when the Python service is unconfigured,
 * unreachable, or rejects), plus the two things this route exists to enforce before it spends
 * anything — the `AGENT_ENABLED` kill switch and the per-user daily message budget — and the
 * conversation rows written around each call.
 *
 * The budget assertions all check `mockFetch` was NOT called. That is the whole point: a 429
 * that still made the upstream request has capped nothing, because the model call is the cost.
 */
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, inject, it, vi } from "vitest";
import { Pool } from "pg";

vi.mock("./require-session", () => ({ requireSession: vi.fn() }));

import { requireSession } from "./require-session";
import { closePools } from "./execute-query";
import { GET, POST } from "../app/api/agent/route";

const mockSession = vi.mocked(requireSession);
const mockFetch = vi.fn();

const SERVICE_URL = "http://agent-service.test";
const SERVICE_TOKEN = "agent-service-token-0123456789abcdef";

const DONE_EVENT =
  'event: done\ndata: {"outcome":"answer","summary":"705 shots.","sql":"SELECT 1",' +
  '"result":{"columns":["n"],"rows":[[705]],"rowCount":1,"truncated":false},"error":null}\n\n';

let owner: Pool;
let userA: number;
let userB: number;

function post(body: unknown) {
  return POST(
    new Request("http://localhost/api/agent", {
      method: "POST",
      body: typeof body === "string" ? body : JSON.stringify(body),
      headers: { "content-type": "application/json" },
    }),
  );
}

function sseStream(events: string) {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(events));
      controller.close();
    },
  });
}

function sseResponse(events: string) {
  return new Response(sseStream(events), {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

/** Fills a user's daily budget with assistant turns that executed no SQL at all — the exact
 * shape `checkRateLimit` is blind to. */
async function spendBudget(userId: number, turns: number) {
  const { rows } = await owner.query(
    "INSERT INTO app.conversation (user_id, title) VALUES ($1, 'spent') RETURNING id",
    [userId],
  );
  for (let i = 0; i < turns; i++) {
    await owner.query(
      `INSERT INTO app.conversation_message (conversation_id, role, content)
       VALUES ($1, 'assistant', '{}'::jsonb)`,
      [rows[0].id],
    );
  }
}

beforeAll(async () => {
  process.env.APP_RW_DATABASE_URL = inject("appUrl");
  process.env.SANDBOX_RO_DATABASE_URL = inject("sandboxUrl");
  owner = new Pool({ connectionString: inject("ownerUrl"), max: 2 });
  const { rows } = await owner.query(
    `INSERT INTO app.users (name) VALUES ('agent-route-a'), ('agent-route-b') RETURNING id`,
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
  vi.stubGlobal("fetch", mockFetch);
  mockFetch.mockReset();
  process.env.AGENT_SERVICE_URL = SERVICE_URL;
  process.env.AGENT_SERVICE_TOKEN = SERVICE_TOKEN;
  process.env.AGENT_ENABLED = "true";
  process.env.AGENT_DAILY_MESSAGE_LIMIT = "100";
});

afterEach(async () => {
  vi.unstubAllGlobals();
  delete process.env.AGENT_SERVICE_URL;
  delete process.env.AGENT_SERVICE_TOKEN;
  delete process.env.AGENT_ENABLED;
  delete process.env.AGENT_DAILY_MESSAGE_LIMIT;
  await owner.query("DELETE FROM app.conversation WHERE user_id = ANY($1::int[])", [
    [userA, userB],
  ]);
});

describe("auth gate", () => {
  it("returns 401 with no session and never calls the agent service", async () => {
    mockSession.mockResolvedValue(null);
    const res = await post({ question: "Who led the league in points?" });
    expect(res.status).toBe(401);
    expect(mockFetch).not.toHaveBeenCalled();
  });
});

describe("kill switch", () => {
  it("returns a clean 503 and never calls upstream when AGENT_ENABLED is unset", async () => {
    delete process.env.AGENT_ENABLED;
    const res = await post({ question: "q" });
    expect(res.status).toBe(503);
    expect((await res.json()).error).toMatch(/disabled/i);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("returns 503 when AGENT_ENABLED is explicitly off", async () => {
    for (const value of ["false", "0", "off", ""]) {
      process.env.AGENT_ENABLED = value;
      expect((await post({ question: "q" })).status).toBe(503);
    }
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("writes no conversation rows while disabled", async () => {
    delete process.env.AGENT_ENABLED;
    await post({ question: "q" });
    const { rows } = await owner.query(
      "SELECT count(*)::int AS n FROM app.conversation WHERE user_id = $1",
      [userA],
    );
    expect(rows[0].n).toBe(0);
  });
});

describe("daily budget", () => {
  it("returns 429 without calling upstream once the cap is reached", async () => {
    process.env.AGENT_DAILY_MESSAGE_LIMIT = "2";
    await spendBudget(userA, 2);

    const res = await post({ question: "q" });
    expect(res.status).toBe(429);
    expect(mockFetch).not.toHaveBeenCalled();

    const body = await res.json();
    expect(body.error).toMatch(/daily agent limit/i);
    expect(body.retryAfterSeconds).toBeGreaterThan(0);
    expect(res.headers.get("Retry-After")).toBe(String(body.retryAfterSeconds));
  });

  it("counts turns that executed no SQL — the case query_log cannot see", async () => {
    process.env.AGENT_DAILY_MESSAGE_LIMIT = "1";
    await spendBudget(userA, 1);

    const { rows } = await owner.query(
      "SELECT count(*)::int AS n FROM app.query_log WHERE user_id = $1",
      [userA],
    );
    expect(rows[0].n).toBe(0);
    expect((await post({ question: "q" })).status).toBe(429);
  });

  it("is per user: user A being over budget does not block user B", async () => {
    process.env.AGENT_DAILY_MESSAGE_LIMIT = "1";
    await spendBudget(userA, 5);
    expect((await post({ question: "q" })).status).toBe(429);

    mockSession.mockResolvedValue({ userId: userB });
    mockFetch.mockResolvedValue(sseResponse(DONE_EVENT));
    const res = await post({ question: "q" });
    expect(res.status).toBe(200);
    expect(mockFetch).toHaveBeenCalledTimes(1);
    await res.text();
  });

  it("passes an under-budget request straight through", async () => {
    process.env.AGENT_DAILY_MESSAGE_LIMIT = "3";
    await spendBudget(userA, 2);
    mockFetch.mockResolvedValue(sseResponse(DONE_EVENT));

    const res = await post({ question: "q" });
    expect(res.status).toBe(200);
    expect(mockFetch).toHaveBeenCalledTimes(1);
    await res.text();
  });

  it("blocks everything at a limit of 0", async () => {
    process.env.AGENT_DAILY_MESSAGE_LIMIT = "0";
    expect((await post({ question: "q" })).status).toBe(429);
    expect(mockFetch).not.toHaveBeenCalled();
  });
});

describe("GET (status)", () => {
  it("requires a session", async () => {
    mockSession.mockResolvedValue(null);
    expect((await GET()).status).toBe(401);
  });

  it("reports the kill switch without touching the budget", async () => {
    delete process.env.AGENT_ENABLED;
    expect(await (await GET()).json()).toMatchObject({ enabled: false, remaining: 0 });
  });

  it("reports remaining budget for the signed-in user", async () => {
    process.env.AGENT_DAILY_MESSAGE_LIMIT = "5";
    await spendBudget(userA, 2);
    expect(await (await GET()).json()).toMatchObject({
      enabled: true,
      used: 2,
      limit: 5,
      remaining: 3,
    });
  });
});

describe("request body", () => {
  it("rejects non-JSON bodies without calling the service", async () => {
    const res = await post("not json");
    expect(res.status).toBe(400);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("rejects a missing question", async () => {
    const res = await post({});
    expect(res.status).toBe(400);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("rejects a blank question", async () => {
    const res = await post({ question: "   " });
    expect(res.status).toBe(400);
    expect(mockFetch).not.toHaveBeenCalled();
  });
});

describe("userId substitution", () => {
  it("forwards the session's userId, ignoring any userId the body claims", async () => {
    mockFetch.mockResolvedValue(sseResponse(DONE_EVENT));

    const res = await post({ question: "How many threes were made?", userId: 999, user_id: 999 });
    await res.text();

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toBe(`${SERVICE_URL}/agent`);
    const sentBody = JSON.parse(init.body as string);
    expect(sentBody.user_id).toBe(userA);
    expect(sentBody.question).toBe("How many threes were made?");
  });

  it("sends the service token as a Bearer header, never in the response", async () => {
    mockFetch.mockResolvedValue(sseResponse(DONE_EVENT));

    const res = await post({ question: "q" });

    const headers = mockFetch.mock.calls[0][1].headers as Record<string, string>;
    expect(headers.authorization).toBe(`Bearer ${SERVICE_TOKEN}`);

    const text = await res.text();
    expect(text).not.toContain(SERVICE_TOKEN);
    expect(JSON.stringify([...res.headers.entries()])).not.toContain(SERVICE_TOKEN);
  });
});

describe("conversation persistence", () => {
  it("creates a titled conversation and both turns for a first question", async () => {
    mockFetch.mockResolvedValue(sseResponse(DONE_EVENT));

    const res = await post({ question: "How many threes were made?" });
    const conversationId = Number(res.headers.get("x-conversation-id"));
    const messageId = Number(res.headers.get("x-conversation-message-id"));
    expect(conversationId).toBeGreaterThan(0);
    expect(messageId).toBeGreaterThan(0);
    await res.text();

    const { rows } = await owner.query(
      "SELECT title, user_id FROM app.conversation WHERE id = $1",
      [conversationId],
    );
    expect(rows[0]).toMatchObject({ user_id: userA, title: "How many threes were made?" });

    const messages = await owner.query(
      "SELECT role, content FROM app.conversation_message WHERE conversation_id = $1 ORDER BY id",
      [conversationId],
    );
    expect(messages.rows.map((r) => r.role)).toEqual(["user", "assistant"]);
    expect(messages.rows[0].content).toEqual({ text: "How many threes were made?" });
  });

  it("finalises the assistant turn with the envelope once the stream ends", async () => {
    mockFetch.mockResolvedValue(sseResponse(`event: node\ndata: {"node":"classify"}\n\n${DONE_EVENT}`));

    const res = await post({ question: "q" });
    const messageId = Number(res.headers.get("x-conversation-message-id"));
    await res.text();

    const { rows } = await owner.query(
      "SELECT content FROM app.conversation_message WHERE id = $1",
      [messageId],
    );
    expect(rows[0].content).toMatchObject({
      outcome: "answer",
      summary: "705 shots.",
      sql: "SELECT 1",
    });
    expect(rows[0].content.result).toMatchObject({ status: "ok", rowCount: 1 });
  });

  it("records an interrupted turn when the stream ends with no done event", async () => {
    mockFetch.mockResolvedValue(sseResponse('event: node\ndata: {"node":"classify"}\n\n'));

    const res = await post({ question: "q" });
    const messageId = Number(res.headers.get("x-conversation-message-id"));
    await res.text();

    const { rows } = await owner.query(
      "SELECT content FROM app.conversation_message WHERE id = $1",
      [messageId],
    );
    expect(rows[0].content.error).toMatch(/without an answer/);
  });

  it("appends to an existing conversation instead of creating another", async () => {
    mockFetch.mockResolvedValue(sseResponse(DONE_EVENT));
    const first = await post({ question: "first" });
    const conversationId = Number(first.headers.get("x-conversation-id"));
    await first.text();

    mockFetch.mockResolvedValue(sseResponse(DONE_EVENT));
    const second = await post({ question: "second", conversationId });
    expect(Number(second.headers.get("x-conversation-id"))).toBe(conversationId);
    await second.text();

    const { rows } = await owner.query(
      "SELECT count(*)::int AS n FROM app.conversation_message WHERE conversation_id = $1",
      [conversationId],
    );
    expect(rows[0].n).toBe(4);
  });

  it("404s rather than writing into a conversation the user does not own", async () => {
    const { rows } = await owner.query(
      "INSERT INTO app.conversation (user_id, title) VALUES ($1, 'theirs') RETURNING id",
      [userB],
    );
    const res = await post({ question: "sneaky", conversationId: Number(rows[0].id) });
    expect(res.status).toBe(404);
    expect(mockFetch).not.toHaveBeenCalled();

    const messages = await owner.query(
      "SELECT count(*)::int AS n FROM app.conversation_message WHERE conversation_id = $1",
      [rows[0].id],
    );
    expect(messages.rows[0].n).toBe(0);
  });

  it("forwards the assistant message id so query_log can be linked to the turn", async () => {
    mockFetch.mockResolvedValue(sseResponse(DONE_EVENT));
    const res = await post({ question: "q" });
    const messageId = Number(res.headers.get("x-conversation-message-id"));
    await res.text();

    const sent = JSON.parse(mockFetch.mock.calls[0][1].body as string);
    expect(sent.conversation_message_id).toBe(messageId);
    expect(sent.conversation_id).toBe(res.headers.get("x-conversation-id"));
  });
});

describe("service misconfiguration", () => {
  it("returns a clean 500 when AGENT_SERVICE_URL is unset, without calling fetch", async () => {
    delete process.env.AGENT_SERVICE_URL;
    const res = await post({ question: "q" });
    expect(res.status).toBe(500);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("returns a clean 500 when AGENT_SERVICE_TOKEN is unset, without calling fetch", async () => {
    delete process.env.AGENT_SERVICE_TOKEN;
    const res = await post({ question: "q" });
    expect(res.status).toBe(500);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("writes no conversation rows when the service is unconfigured", async () => {
    delete process.env.AGENT_SERVICE_URL;
    await post({ question: "q" });
    const { rows } = await owner.query(
      "SELECT count(*)::int AS n FROM app.conversation WHERE user_id = $1",
      [userA],
    );
    expect(rows[0].n).toBe(0);
  });
});

describe("service unreachable", () => {
  it("returns a clean 502 instead of hanging when fetch rejects", async () => {
    mockFetch.mockRejectedValue(new TypeError("fetch failed"));
    const res = await post({ question: "q" });
    expect(res.status).toBe(502);
    const body = await res.json();
    expect(body.error).toMatch(/agent service unreachable/);
  });

  it("marks the started turn as failed rather than leaving it pending", async () => {
    mockFetch.mockRejectedValue(new TypeError("fetch failed"));
    await post({ question: "q" });

    const { rows } = await owner.query(
      `SELECT content FROM app.conversation_message
        WHERE role = 'assistant' ORDER BY id DESC LIMIT 1`,
    );
    expect(rows[0].content.error).toMatch(/unreachable/);
    expect(rows[0].content.pending).toBeUndefined();
  });
});

describe("upstream non-2xx", () => {
  it("forwards a validation-style error from the service without hanging", async () => {
    mockFetch.mockResolvedValue(
      new Response(JSON.stringify({ detail: "invalid request" }), { status: 422 }),
    );
    const res = await post({ question: "q" });
    expect(res.status).toBe(422);
    const body = await res.json();
    expect(body.error).toContain("invalid request");
  });
});

describe("streaming", () => {
  it("streams the upstream SSE body through unmodified with the right content-type", async () => {
    const payload = `event: node\ndata: {"node":"classify"}\n\n${DONE_EVENT}`;
    mockFetch.mockResolvedValue(sseResponse(payload));

    const res = await post({ question: "q" });
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toBe("text/event-stream");
    const text = await res.text();
    expect(text).toBe(payload);
  });
});
