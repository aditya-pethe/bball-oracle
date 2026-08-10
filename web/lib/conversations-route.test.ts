/**
 * The conversation CRUD routes and the persistence helpers behind them.
 *
 * Two things are being defended here. Ownership: every read and every write is scoped by
 * `user_id` in SQL, so naming someone else's conversation id gets a 404 rather than their
 * thread. And the storage constraint from db/migrations/0004's own column comment: a persisted
 * assistant turn never holds a full QueryResult.
 */
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, inject, it, vi } from "vitest";
import { Pool } from "pg";

vi.mock("./require-session", () => ({ requireSession: vi.fn() }));

import { requireSession } from "./require-session";
import { closePools } from "./execute-query";
import {
  MAX_PREVIEW_ROWS,
  appendAssistantPlaceholder,
  appendUserMessage,
  finalizeAssistantMessage,
  titleFrom,
  toStoredEnvelope,
} from "./conversations";
import { GET as listConversations, POST as createConversation } from "../app/api/conversations/route";
import { DELETE as deleteOne, GET as getOne } from "../app/api/conversations/[id]/route";
import type { AgentEnvelope } from "./api-types";

const mockSession = vi.mocked(requireSession);

let owner: Pool;
let userA: number;
let userB: number;

function ctx(id: string | number) {
  return { params: Promise.resolve({ id: String(id) }) };
}

function postRequest(body?: unknown) {
  return new Request("http://localhost/api/conversations", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

async function insertConversation(userId: number, title: string | null): Promise<number> {
  const { rows } = await owner.query(
    "INSERT INTO app.conversation (user_id, title) VALUES ($1, $2) RETURNING id",
    [userId, title],
  );
  return Number(rows[0].id);
}

function okEnvelope(rowCount: number): AgentEnvelope {
  return {
    outcome: "answer",
    summary: `${rowCount} rows.`,
    sql: "SELECT 1",
    result: {
      status: "ok",
      columns: ["n"],
      rows: Array.from({ length: rowCount }, (_, i) => [i]),
      rowCount,
      truncated: false,
      durationMs: 10,
    },
    error: null,
  };
}

beforeAll(async () => {
  process.env.APP_RW_DATABASE_URL = inject("appUrl");
  process.env.SANDBOX_RO_DATABASE_URL = inject("sandboxUrl");
  owner = new Pool({ connectionString: inject("ownerUrl"), max: 2 });
  const { rows } = await owner.query(
    `INSERT INTO app.users (name) VALUES ('conv-route-a'), ('conv-route-b') RETURNING id`,
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

afterEach(async () => {
  await owner.query("DELETE FROM app.conversation WHERE user_id = ANY($1::int[])", [
    [userA, userB],
  ]);
});

describe("auth gate", () => {
  it("returns 401 from every conversation route with no session", async () => {
    mockSession.mockResolvedValue(null);
    expect((await listConversations()).status).toBe(401);
    expect((await createConversation(postRequest({}))).status).toBe(401);
    expect((await getOne(new Request("http://localhost"), ctx(1))).status).toBe(401);
    expect((await deleteOne(new Request("http://localhost"), ctx(1))).status).toBe(401);
  });
});

describe("GET /api/conversations", () => {
  it("returns only the signed-in user's conversations", async () => {
    await insertConversation(userA, "mine");
    await insertConversation(userB, "theirs");

    const body = await (await listConversations()).json();
    const titles = body.conversations.map((c: { title: string }) => c.title);
    expect(titles).toContain("mine");
    expect(titles).not.toContain("theirs");
  });

  it("orders most recently updated first and normalises ids to numbers", async () => {
    const older = await insertConversation(userA, "older");
    const newer = await insertConversation(userA, "newer");
    await owner.query("UPDATE app.conversation SET updated_at = now() - interval '1 day' WHERE id = $1", [
      older,
    ]);

    const body = await (await listConversations()).json();
    expect(body.conversations[0].id).toBe(newer);
    expect(typeof body.conversations[0].id).toBe("number");
    expect(body.conversations.map((c: { title: string }) => c.title)).toEqual(["newer", "older"]);
  });
});

describe("POST /api/conversations", () => {
  it("creates an untitled conversation owned by the session user", async () => {
    const res = await createConversation(postRequest({}));
    expect(res.status).toBe(201);
    const { conversation } = await res.json();
    expect(conversation.title).toBeNull();

    const { rows } = await owner.query("SELECT user_id FROM app.conversation WHERE id = $1", [
      conversation.id,
    ]);
    expect(rows[0].user_id).toBe(userA);
  });

  it("tolerates a missing body", async () => {
    expect((await createConversation(postRequest())).status).toBe(201);
  });

  it("truncates an over-long title", async () => {
    const res = await createConversation(postRequest({ title: "x".repeat(200) }));
    const { conversation } = await res.json();
    expect(conversation.title.length).toBeLessThanOrEqual(60);
  });
});

describe("GET /api/conversations/[id]", () => {
  it("returns the conversation with its messages in order", async () => {
    const id = await insertConversation(userA, "thread");
    await appendUserMessage(userA, id, "how many threes?");
    const assistantId = await appendAssistantPlaceholder(userA, id);
    await finalizeAssistantMessage(assistantId as number, okEnvelope(2));

    const body = await (await getOne(new Request("http://localhost"), ctx(id))).json();
    expect(body.conversation.id).toBe(id);
    expect(body.messages).toHaveLength(2);
    expect(body.messages[0]).toMatchObject({ role: "user", text: "how many threes?" });
    expect(body.messages[1].role).toBe("assistant");
    expect(body.messages[1].envelope.summary).toBe("2 rows.");
  });

  it("renders an unfinished assistant turn as an error, not an empty answer", async () => {
    const id = await insertConversation(userA, "interrupted");
    await appendAssistantPlaceholder(userA, id);

    const body = await (await getOne(new Request("http://localhost"), ctx(id))).json();
    expect(body.messages[0].envelope.error).toMatch(/interrupted/);
  });

  it("404s on another user's conversation", async () => {
    const id = await insertConversation(userB, "theirs");
    const res = await getOne(new Request("http://localhost"), ctx(id));
    expect(res.status).toBe(404);
  });

  it("404s on a nonexistent or non-numeric id", async () => {
    expect((await getOne(new Request("http://localhost"), ctx(987654321))).status).toBe(404);
    expect((await getOne(new Request("http://localhost"), ctx("not-an-id"))).status).toBe(404);
  });
});

describe("DELETE /api/conversations/[id]", () => {
  it("deletes the caller's own conversation and its messages", async () => {
    const id = await insertConversation(userA, "doomed");
    await appendUserMessage(userA, id, "q");

    expect((await deleteOne(new Request("http://localhost"), ctx(id))).status).toBe(200);
    const { rows } = await owner.query("SELECT 1 FROM app.conversation WHERE id = $1", [id]);
    expect(rows).toHaveLength(0);
    const messages = await owner.query(
      "SELECT 1 FROM app.conversation_message WHERE conversation_id = $1",
      [id],
    );
    expect(messages.rows).toHaveLength(0);
  });

  it("404s on another user's conversation and leaves it intact", async () => {
    const id = await insertConversation(userB, "theirs");
    expect((await deleteOne(new Request("http://localhost"), ctx(id))).status).toBe(404);
    const { rows } = await owner.query("SELECT 1 FROM app.conversation WHERE id = $1", [id]);
    expect(rows).toHaveLength(1);
  });
});

describe("message writes are ownership-scoped", () => {
  it("refuses to append to a conversation the user does not own", async () => {
    const id = await insertConversation(userB, "theirs");
    expect(await appendUserMessage(userA, id, "sneaky")).toBeNull();
    expect(await appendAssistantPlaceholder(userA, id)).toBeNull();

    const { rows } = await owner.query(
      "SELECT count(*)::int AS n FROM app.conversation_message WHERE conversation_id = $1",
      [id],
    );
    expect(rows[0].n).toBe(0);
  });
});

describe("storage constraint (db/migrations/0004)", () => {
  it("caps a stored result at 50 preview rows and marks it truncated", () => {
    const stored = toStoredEnvelope(okEnvelope(1000));
    expect(stored.result).toMatchObject({ status: "ok", truncated: true });
    expect((stored.result as { rows: unknown[] }).rows).toHaveLength(MAX_PREVIEW_ROWS);
  });

  it("leaves a small result untouched, including its truncated flag", () => {
    const stored = toStoredEnvelope(okEnvelope(3));
    expect((stored.result as { rows: unknown[] }).rows).toHaveLength(3);
    expect(stored.result).toMatchObject({ truncated: false });
  });

  it("persists no more than the preview, even for a full 1000-row result", async () => {
    const id = await insertConversation(userA, "big");
    const assistantId = (await appendAssistantPlaceholder(userA, id)) as number;
    await finalizeAssistantMessage(assistantId, okEnvelope(1000));

    const { rows } = await owner.query(
      "SELECT jsonb_array_length(content -> 'result' -> 'rows') AS n FROM app.conversation_message WHERE id = $1",
      [assistantId],
    );
    expect(Number(rows[0].n)).toBe(MAX_PREVIEW_ROWS);
  });

  it("keeps a failure envelope's error intact (nothing to preview)", () => {
    const stored = toStoredEnvelope({
      outcome: "answer",
      summary: "",
      sql: "SELECT bad",
      result: { status: "error", error: "boom", durationMs: 5 },
      error: "boom",
    });
    expect(stored.result).toMatchObject({ status: "error", error: "boom" });
  });
});

describe("titleFrom", () => {
  it("collapses whitespace and truncates with an ellipsis", () => {
    expect(titleFrom("  how   many\nthrees?  ")).toBe("how many threes?");
    const long = titleFrom("y".repeat(200));
    expect(long).toHaveLength(60);
    expect(long.endsWith("…")).toBe(true);
  });
});
