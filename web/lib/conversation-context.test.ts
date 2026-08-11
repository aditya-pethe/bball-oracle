/**
 * The TypeScript half of the conversation-context contract
 * (.agents/p5_conversational_context.md). Pure projection — no database, no
 * network: persisted turns in, the bounded payload the Python service receives out.
 *
 * The Pydantic half is `agent/context.py` and the two must agree field for field;
 * `agent/tests/test_context.py` asserts the same bounds from the other side.
 */
import { describe, expect, it } from "vitest";
import {
  MAX_CONTEXT_MESSAGES,
  MAX_PREVIEW_ROWS,
  MAX_SERIALIZED_BYTES,
  buildConversationContext,
  pendingClarification,
} from "./conversation-context";
import type { AgentEnvelope, AgentMessage, OkResult } from "./api-types";

let nextId = 1;

function userMessage(text: string): AgentMessage {
  return { id: nextId++, role: "user", createdAt: new Date().toISOString(), text };
}

function okResult(rows: unknown[][], columns = ["n"]): OkResult {
  return {
    status: "ok",
    columns,
    rows,
    rowCount: rows.length,
    truncated: false,
    durationMs: 12,
  };
}

function assistantMessage(envelope: Partial<AgentEnvelope>): AgentMessage {
  return {
    id: nextId++,
    role: "assistant",
    createdAt: new Date().toISOString(),
    envelope: {
      outcome: "answer",
      summary: "",
      sql: null,
      result: null,
      error: null,
      ...envelope,
    },
  };
}

function exchange(question: string, summary: string, sql: string): AgentMessage[] {
  return [
    userMessage(question),
    assistantMessage({ summary, sql, result: okResult([[1]]) }),
  ];
}

describe("projection", () => {
  it("maps a user turn to its text", () => {
    const context = buildConversationContext([userMessage("How many threes?")]);
    expect(context.turns).toEqual([{ role: "user", text: "How many threes?" }]);
  });

  it("maps an answered assistant turn to summary, sql, shape and a preview", () => {
    const context = buildConversationContext([
      assistantMessage({
        summary: "Curry made 357.",
        sql: "SELECT 1",
        result: okResult([[357]], ["made_threes"]),
      }),
    ]);
    expect(context.turns[0]).toEqual({
      role: "assistant",
      text: "Curry made 357.",
      outcome: "answer",
      sql: "SELECT 1",
      result_shape: { columns: ["made_threes"], row_count: 1, truncated: false },
      result_preview: [[357]],
    });
  });

  it("keeps clarify and decline outcomes", () => {
    const context = buildConversationContext([
      assistantMessage({ outcome: "clarify", summary: "By what measure?" }),
      assistantMessage({ outcome: "decline", summary: "No playoff data." }),
    ]);
    expect(context.turns.map((t) => t.outcome)).toEqual(["clarify", "decline"]);
  });

  it("drops the SQL of a turn that failed, so a follow-up cannot transform it", () => {
    const context = buildConversationContext([
      assistantMessage({ summary: "", sql: "SELECT bad", error: "syntax error" }),
    ]);
    expect(context.turns[0].sql).toBeUndefined();
    expect(context.turns[0].text).toContain("syntax error");
  });

  it("drops the SQL of a turn whose result was a query failure", () => {
    const context = buildConversationContext([
      assistantMessage({
        summary: "",
        sql: "SELECT 1",
        result: { status: "timeout", error: "canceled", durationMs: 5000 },
      }),
    ]);
    expect(context.turns[0].sql).toBeUndefined();
    expect(context.turns[0].result_shape).toBeUndefined();
  });

  it("never carries durationMs or any other instrumentation into the payload", () => {
    const context = buildConversationContext([
      assistantMessage({ summary: "ok", sql: "SELECT 1", result: okResult([[1]]) }),
    ]);
    expect(JSON.stringify(context)).not.toContain("durationMs");
    expect(JSON.stringify(context)).not.toContain("status");
  });
});

describe("bounds", () => {
  it("keeps only the most recent MAX_CONTEXT_MESSAGES turns", () => {
    const messages: AgentMessage[] = [];
    for (let i = 0; i < 20; i++) messages.push(...exchange(`q${i}`, `a${i}`, "SELECT 1"));
    const context = buildConversationContext(messages);
    expect(context.turns).toHaveLength(MAX_CONTEXT_MESSAGES);
    expect(context.turns[context.turns.length - 1].text).toBe("a19");
  });

  it("caps preview rows without rewriting the real row count", () => {
    const rows = Array.from({ length: 40 }, (_, i) => [i]);
    const context = buildConversationContext([
      assistantMessage({ summary: "ok", sql: "SELECT 1", result: okResult(rows) }),
    ]);
    expect(context.turns[0].result_preview).toHaveLength(MAX_PREVIEW_ROWS);
    expect(context.turns[0].result_shape?.row_count).toBe(40);
  });

  it("enforces a serialized-size ceiling by dropping the oldest turns", () => {
    const big = "x".repeat(MAX_SERIALIZED_BYTES / 3);
    const messages = [
      userMessage(big),
      assistantMessage({ summary: big }),
      userMessage(big),
      assistantMessage({ summary: "recent" }),
    ];
    const context = buildConversationContext(messages);
    expect(JSON.stringify(context).length).toBeLessThanOrEqual(MAX_SERIALIZED_BYTES);
    expect(context.turns[context.turns.length - 1].text).toBe("recent");
  });

  it("truncates rather than deleting when a single turn exceeds the ceiling", () => {
    const context = buildConversationContext([
      assistantMessage({ summary: "y".repeat(MAX_SERIALIZED_BYTES * 3) }),
    ]);
    expect(context.turns).toHaveLength(1);
    expect(JSON.stringify(context).length).toBeLessThanOrEqual(MAX_SERIALIZED_BYTES);
  });

  it("produces an empty context for an empty thread", () => {
    expect(buildConversationContext([])).toEqual({ turns: [] });
  });
});

describe("pending clarification", () => {
  it("is null when the last assistant turn answered", () => {
    const context = buildConversationContext(exchange("q", "a", "SELECT 1"));
    expect(pendingClarification(context)).toBeNull();
  });

  it("is detected when the last assistant turn clarified", () => {
    const context = buildConversationContext([
      userMessage("Who was most clutch?"),
      assistantMessage({ outcome: "clarify", summary: "How do you define clutch?" }),
    ]);
    expect(pendingClarification(context)).toEqual({
      originalQuestion: "Who was most clutch?",
      clarifyQuestion: "How do you define clutch?",
    });
  });

  it("is closed by a later answer", () => {
    const context = buildConversationContext([
      userMessage("Who was most clutch?"),
      assistantMessage({ outcome: "clarify", summary: "Define clutch?" }),
      userMessage("last two minutes"),
      assistantMessage({ summary: "Here you go.", sql: "SELECT 1", result: okResult([[1]]) }),
    ]);
    expect(pendingClarification(context)).toBeNull();
  });

  it("does not also duplicate the pinned pair inside the recent window", () => {
    // The clarify turn is the LAST assistant turn, so a long run of user turns
    // after it puts it inside the recent window as well as in the pin.
    const messages: AgentMessage[] = [
      ...exchange("filler question", "filler answer", "SELECT 1"),
      userMessage("the original question"),
      assistantMessage({ outcome: "clarify", summary: "which one?" }),
    ];
    for (let i = 0; i < 5; i++) messages.push(userMessage(`noise${i}`));

    const context = buildConversationContext(messages);
    expect(context.turns.filter((t) => t.outcome === "clarify")).toHaveLength(1);
    expect(context.turns.length).toBeLessThanOrEqual(MAX_CONTEXT_MESSAGES);
  });

  it("retains the pending exchange when truncation would otherwise drop it", () => {
    const messages: AgentMessage[] = [
      userMessage("the original question"),
      assistantMessage({ outcome: "clarify", summary: "which one?" }),
    ];
    // A long run of user turns with no completed assistant reply would push the
    // pair out of the recent window.
    for (let i = 0; i < 20; i++) messages.push(userMessage(`noise${i}`));

    const context = buildConversationContext(messages);
    expect(context.turns.length).toBeLessThanOrEqual(MAX_CONTEXT_MESSAGES);
    expect(context.turns[0].text).toBe("the original question");
    expect(context.turns[1].outcome).toBe("clarify");
    expect(pendingClarification(context)?.originalQuestion).toBe("the original question");
  });
});
