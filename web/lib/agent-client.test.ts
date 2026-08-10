import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  AgentClientError,
  NODE_LABELS,
  streamAgentAnswer,
  toAgentEnvelope,
  toQueryResult,
  type RawAgentEnvelope,
} from "./agent-client";

const mockFetch = vi.fn();

beforeEach(() => {
  vi.stubGlobal("fetch", mockFetch);
  mockFetch.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function streamFrom(chunks: string[]) {
  const encoder = new TextEncoder();
  let i = 0;
  return new ReadableStream({
    pull(controller) {
      if (i < chunks.length) {
        controller.enqueue(encoder.encode(chunks[i++]));
      } else {
        controller.close();
      }
    },
  });
}

function sseResponse(chunks: string[], headers: Record<string, string> = {}) {
  return new Response(streamFrom(chunks), {
    status: 200,
    headers: { "content-type": "text/event-stream", ...headers },
  });
}

async function collect<T>(gen: AsyncGenerator<T>): Promise<T[]> {
  const out: T[] = [];
  for await (const item of gen) out.push(item);
  return out;
}

describe("toQueryResult", () => {
  it("maps a present result into an OkResult with the client-measured duration", () => {
    const raw: RawAgentEnvelope = {
      outcome: "answer",
      summary: "s",
      sql: "SELECT 1",
      result: { columns: ["n"], rows: [[1]], rowCount: 1, truncated: false },
      error: null,
    };
    expect(toQueryResult(raw, 123)).toEqual({
      status: "ok",
      columns: ["n"],
      rows: [[1]],
      rowCount: 1,
      truncated: false,
      durationMs: 123,
    });
  });

  it("maps an error with no result into a QueryResult error", () => {
    const raw: RawAgentEnvelope = {
      outcome: "answer",
      summary: "",
      sql: "SELECT bad",
      result: null,
      error: "relation does not exist",
    };
    expect(toQueryResult(raw, 50)).toEqual({
      status: "error",
      error: "relation does not exist",
      durationMs: 50,
    });
  });

  it("returns null for clarify/decline (no result, no error)", () => {
    const raw: RawAgentEnvelope = {
      outcome: "clarify",
      summary: "Which season?",
      sql: null,
      result: null,
      error: null,
    };
    expect(toQueryResult(raw, 10)).toBeNull();
  });
});

describe("toAgentEnvelope", () => {
  it("carries outcome/summary/sql through and maps result via toQueryResult", () => {
    const raw: RawAgentEnvelope = {
      outcome: "answer",
      summary: "705 shots.",
      sql: "SELECT COUNT(*) FROM nba.shot_detail",
      result: { columns: ["n"], rows: [[705]], rowCount: 1, truncated: false },
      error: null,
    };
    const envelope = toAgentEnvelope(raw, 99);
    expect(envelope.outcome).toBe("answer");
    expect(envelope.summary).toBe("705 shots.");
    expect(envelope.sql).toBe(raw.sql);
    expect(envelope.result).toEqual({
      status: "ok",
      columns: ["n"],
      rows: [[705]],
      rowCount: 1,
      truncated: false,
      durationMs: 99,
    });
    expect(envelope.error).toBeNull();
  });
});

describe("NODE_LABELS", () => {
  it("has a label for every node in the graph", () => {
    for (const node of ["classify", "clarify", "decline", "draft_sql", "execute", "critic", "summarize"]) {
      expect(NODE_LABELS[node]).toBeTruthy();
    }
  });
});

describe("streamAgentAnswer", () => {
  it("yields node events then a done event with the mapped envelope", async () => {
    mockFetch.mockResolvedValue(
      sseResponse([
        'event: node\ndata: {"node":"classify","node_timings":[{"node":"classify","duration_ms":12.5}]}\n\n',
        'event: node\ndata: {"node":"draft_sql","node_timings":[{"node":"draft_sql","duration_ms":340}]}\n\n',
        'event: done\ndata: {"outcome":"answer","summary":"705 shots.","sql":"SELECT COUNT(*) FROM nba.shot_detail","result":{"columns":["n"],"rows":[[705]],"rowCount":1,"truncated":false},"error":null}\n\n',
      ]),
    );

    const events = await collect(streamAgentAnswer({ question: "How many shots?" }));

    expect(events).toHaveLength(4);
    expect(events[0].type).toBe("meta");
    expect(events[1]).toEqual({ type: "node", node: "classify", durationMs: 12.5 });
    expect(events[2]).toEqual({ type: "node", node: "draft_sql", durationMs: 340 });
    expect(events[3].type).toBe("done");
    const done = events[3] as { type: "done"; envelope: { outcome: string; result: unknown } };
    expect(done.envelope.outcome).toBe("answer");
    expect(done.envelope.result).toMatchObject({ status: "ok", rowCount: 1 });
  });

  it("reports the persisted ids from the response headers as the first event", async () => {
    mockFetch.mockResolvedValue(
      sseResponse(
        ['event: done\ndata: {"outcome":"answer","summary":"s","sql":null,"result":null,"error":null}\n\n'],
        { "x-conversation-id": "7", "x-conversation-message-id": "31" },
      ),
    );

    const events = await collect(streamAgentAnswer({ question: "q" }));
    expect(events[0]).toEqual({ type: "meta", conversationId: 7, conversationMessageId: 31 });
  });

  it("yields a meta event with null ids when the headers are absent", async () => {
    mockFetch.mockResolvedValue(
      sseResponse(['event: done\ndata: {"outcome":"answer","summary":"s","sql":null,"result":null,"error":null}\n\n']),
    );

    const events = await collect(streamAgentAnswer({ question: "q" }));
    expect(events[0]).toEqual({ type: "meta", conversationId: null, conversationMessageId: null });
  });

  it("reassembles an SSE event split across multiple stream chunks", async () => {
    mockFetch.mockResolvedValue(
      sseResponse([
        'event: nod',
        'e\ndata: {"nod',
        'e":"classify"}\n',
        '\n',
        'event: done\ndata: {"outcome":"decline","summary":"cannot answer","sql":null,"result":null,"error":null}\n\n',
      ]),
    );

    const events = await collect(streamAgentAnswer({ question: "q" }));
    expect(events[1]).toEqual({ type: "node", node: "classify", durationMs: null });
    expect((events[2] as { type: "done"; envelope: { outcome: string } }).envelope.outcome).toBe(
      "decline",
    );
  });

  it("sends the question and conversationId in the POST body", async () => {
    mockFetch.mockResolvedValue(
      sseResponse(['event: done\ndata: {"outcome":"answer","summary":"s","sql":null,"result":null,"error":null}\n\n']),
    );
    await collect(streamAgentAnswer({ question: "How many threes?", conversationId: 12 }));

    expect(mockFetch).toHaveBeenCalledWith(
      "/api/agent",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ question: "How many threes?", conversationId: 12 }),
      }),
    );
  });

  it("throws AgentClientError with the server's message and status on a non-ok response", async () => {
    mockFetch.mockResolvedValue(
      new Response(JSON.stringify({ error: "authentication required" }), { status: 401 }),
    );

    await expect(collect(streamAgentAnswer({ question: "q" }))).rejects.toMatchObject({
      name: "AgentClientError",
      message: "authentication required",
      status: 401,
    });
  });

  it("throws AgentClientError when fetch itself rejects (service unreachable)", async () => {
    mockFetch.mockRejectedValue(new TypeError("Failed to fetch"));

    await expect(collect(streamAgentAnswer({ question: "q" }))).rejects.toThrow(AgentClientError);
  });
});
