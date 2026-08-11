/**
 * `readConversationContext` — the authenticated, bounded read that feeds the model
 * (.agents/p5_conversational_context.md step 2).
 *
 * Three properties are being defended, and each of them is a way conversational
 * context can go wrong that nothing downstream would catch:
 *
 *   Ownership — the scope is `(user_id, conversation_id)` in SQL, so naming someone
 *   else's thread yields nothing rather than their questions in your prompt.
 *
 *   In-flight exclusion — the current user message and the assistant placeholder are
 *   written before the upstream call, so a naive read would duplicate the question
 *   and present an empty placeholder as history.
 *
 *   Bounds — a long thread must not grow the prompt without limit.
 */
import { afterAll, afterEach, beforeAll, describe, expect, inject, it } from "vitest";
import { Pool } from "pg";

import { closePools } from "./execute-query";
import {
  appendAssistantPlaceholder,
  appendUserMessage,
  finalizeAssistantMessage,
  readConversationContext,
} from "./conversations";
import { MAX_CONTEXT_MESSAGES } from "./conversation-context";
import type { AgentEnvelope } from "./api-types";

let owner: Pool;
let userA: number;
let userB: number;

async function newConversation(userId: number): Promise<number> {
  const { rows } = await owner.query(
    "INSERT INTO app.conversation (user_id, title) VALUES ($1, 'ctx') RETURNING id",
    [userId],
  );
  return Number(rows[0].id);
}

function answer(summary: string, sql = "SELECT 1"): AgentEnvelope {
  return {
    outcome: "answer",
    summary,
    sql,
    result: {
      status: "ok",
      columns: ["n"],
      rows: [[1]],
      rowCount: 1,
      truncated: false,
      durationMs: 3,
    },
    error: null,
  };
}

/** One completed exchange, written the way `/api/agent` writes it. */
async function completedExchange(userId: number, conversationId: number, question: string, summary: string) {
  await appendUserMessage(userId, conversationId, question);
  const id = await appendAssistantPlaceholder(userId, conversationId);
  await finalizeAssistantMessage(id!, answer(summary));
}

beforeAll(async () => {
  process.env.APP_RW_DATABASE_URL = inject("appUrl");
  process.env.SANDBOX_RO_DATABASE_URL = inject("sandboxUrl");
  owner = new Pool({ connectionString: inject("ownerUrl"), max: 2 });
  const { rows } = await owner.query(
    `INSERT INTO app.users (name) VALUES ('ctx-read-a'), ('ctx-read-b') RETURNING id`,
  );
  userA = rows[0].id;
  userB = rows[1].id;
});

afterAll(async () => {
  await owner.end();
  await closePools();
});

afterEach(async () => {
  await owner.query("DELETE FROM app.conversation WHERE user_id = ANY($1::int[])", [[userA, userB]]);
});

describe("ownership", () => {
  it("returns nothing for a conversation owned by someone else", async () => {
    const theirs = await newConversation(userB);
    await completedExchange(userB, theirs, "their question", "their answer");

    const context = await readConversationContext(userA, theirs);
    expect(context.turns).toEqual([]);
  });

  it("returns nothing for a conversation that does not exist", async () => {
    expect((await readConversationContext(userA, 9_999_999)).turns).toEqual([]);
  });

  it("returns the owner's own turns", async () => {
    const mine = await newConversation(userA);
    await completedExchange(userA, mine, "my question", "my answer");

    const context = await readConversationContext(userA, mine);
    expect(context.turns.map((t) => t.text)).toEqual(["my question", "my answer"]);
  });
});

describe("ordering", () => {
  it("returns turns oldest first", async () => {
    const id = await newConversation(userA);
    await completedExchange(userA, id, "first", "answered first");
    await completedExchange(userA, id, "second", "answered second");

    const context = await readConversationContext(userA, id);
    expect(context.turns.map((t) => t.text)).toEqual([
      "first",
      "answered first",
      "second",
      "answered second",
    ]);
  });
});

describe("in-flight rows", () => {
  it("excludes an unfinished assistant placeholder", async () => {
    const id = await newConversation(userA);
    await completedExchange(userA, id, "done question", "done answer");
    await appendUserMessage(userA, id, "in-flight question");
    await appendAssistantPlaceholder(userA, id);

    const context = await readConversationContext(userA, id);
    // The placeholder is gone; the in-flight *question* is only excluded when the
    // caller says so (see beforeMessageId below), which is what /api/agent does.
    expect(context.turns.filter((t) => t.role === "assistant").map((t) => t.text)).toEqual([
      "done answer",
    ]);
    expect(JSON.stringify(context)).not.toContain("interrupted");
  });

  it("excludes rows at or after beforeMessageId, so the current question is not duplicated", async () => {
    const id = await newConversation(userA);
    await completedExchange(userA, id, "earlier", "earlier answer");
    const currentUserMessageId = await appendUserMessage(userA, id, "the current question");

    const context = await readConversationContext(userA, id, {
      beforeMessageId: currentUserMessageId,
    });
    expect(context.turns.map((t) => t.text)).toEqual(["earlier", "earlier answer"]);
  });

  it("still surfaces a finished turn that recorded an error", async () => {
    // Distinct from a placeholder: this turn completed, it just failed. Hiding it
    // would let a follow-up read as if the failed attempt never happened.
    const id = await newConversation(userA);
    await appendUserMessage(userA, id, "q");
    const assistantId = await appendAssistantPlaceholder(userA, id);
    await finalizeAssistantMessage(assistantId!, {
      outcome: "answer",
      summary: "",
      sql: "SELECT bad",
      result: null,
      error: "column does not exist",
    });

    const context = await readConversationContext(userA, id);
    expect(context.turns).toHaveLength(2);
    expect(context.turns[1].text).toContain("column does not exist");
    expect(context.turns[1].sql).toBeUndefined();
  });
});

describe("bounds", () => {
  it("caps a long thread at the message bound", async () => {
    const id = await newConversation(userA);
    for (let i = 0; i < 12; i++) await completedExchange(userA, id, `q${i}`, `a${i}`);

    const context = await readConversationContext(userA, id);
    expect(context.turns.length).toBeLessThanOrEqual(MAX_CONTEXT_MESSAGES);
    expect(context.turns[context.turns.length - 1].text).toBe("a11");
  });

  it("never carries a full result set out of storage", async () => {
    const id = await newConversation(userA);
    await appendUserMessage(userA, id, "q");
    const assistantId = await appendAssistantPlaceholder(userA, id);
    await finalizeAssistantMessage(assistantId!, {
      outcome: "answer",
      summary: "many rows",
      sql: "SELECT 1",
      result: {
        status: "ok",
        columns: ["n"],
        rows: Array.from({ length: 200 }, (_, i) => [i]),
        rowCount: 200,
        truncated: false,
        durationMs: 3,
      },
      error: null,
    });

    const context = await readConversationContext(userA, id);
    expect(context.turns[1].result_preview!.length).toBeLessThanOrEqual(5);
    // The stored row (already preview-capped at 50 by conversations.ts) reports the
    // count it was persisted with; the point is that no 200-row payload is in the prompt.
    expect(JSON.stringify(context)).not.toContain("[199]");
  });
});
