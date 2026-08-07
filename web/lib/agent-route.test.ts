/**
 * `/api/agent`'s proxy behavior: session gate, userId substitution (never trust the
 * browser body), the service token forwarded but never echoed back, and clean
 * (non-hanging) errors when the Python service is unconfigured, unreachable, or
 * itself rejects the request. No Postgres involved — the route never touches the
 * database itself, it only forwards to the agent service.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./require-session", () => ({ requireSession: vi.fn() }));

import { requireSession } from "./require-session";
import { POST } from "../app/api/agent/route";

const mockSession = vi.mocked(requireSession);
const mockFetch = vi.fn();

const SERVICE_URL = "http://agent-service.test";
const SERVICE_TOKEN = "agent-service-token-0123456789abcdef";

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

beforeEach(() => {
  mockSession.mockResolvedValue({ userId: 42 });
  vi.stubGlobal("fetch", mockFetch);
  mockFetch.mockReset();
  process.env.AGENT_SERVICE_URL = SERVICE_URL;
  process.env.AGENT_SERVICE_TOKEN = SERVICE_TOKEN;
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete process.env.AGENT_SERVICE_URL;
  delete process.env.AGENT_SERVICE_TOKEN;
});

describe("auth gate", () => {
  it("returns 401 with no session and never calls the agent service", async () => {
    mockSession.mockResolvedValue(null);
    const res = await post({ question: "Who led the league in points?" });
    expect(res.status).toBe(401);
    expect(mockFetch).not.toHaveBeenCalled();
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
    mockFetch.mockResolvedValue(
      new Response(sseStream('event: done\ndata: {"outcome":"answer"}\n\n'), {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      }),
    );

    await post({ question: "How many threes were made?", userId: 999, user_id: 999 });

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toBe(`${SERVICE_URL}/agent`);
    const sentBody = JSON.parse(init.body as string);
    expect(sentBody.user_id).toBe(42);
    expect(sentBody.question).toBe("How many threes were made?");
  });

  it("forwards a string conversationId as conversation_id, and null when absent", async () => {
    mockFetch.mockResolvedValue(
      new Response(sseStream('event: done\ndata: {"outcome":"answer"}\n\n'), {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      }),
    );

    await post({ question: "q1", conversationId: "abc-123" });
    expect(JSON.parse(mockFetch.mock.calls[0][1].body as string).conversation_id).toBe("abc-123");

    await post({ question: "q2" });
    expect(JSON.parse(mockFetch.mock.calls[1][1].body as string).conversation_id).toBeNull();
  });

  it("sends the service token as a Bearer header, never in the response", async () => {
    mockFetch.mockResolvedValue(
      new Response(sseStream('event: done\ndata: {"outcome":"answer"}\n\n'), {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      }),
    );

    const res = await post({ question: "q" });

    const headers = mockFetch.mock.calls[0][1].headers as Record<string, string>;
    expect(headers.authorization).toBe(`Bearer ${SERVICE_TOKEN}`);

    const text = await res.text();
    expect(text).not.toContain(SERVICE_TOKEN);
    expect(JSON.stringify([...res.headers.entries()])).not.toContain(SERVICE_TOKEN);
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
});

describe("service unreachable", () => {
  it("returns a clean 502 instead of hanging when fetch rejects", async () => {
    mockFetch.mockRejectedValue(new TypeError("fetch failed"));
    const res = await post({ question: "q" });
    expect(res.status).toBe(502);
    const body = await res.json();
    expect(body.error).toMatch(/agent service unreachable/);
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
    const payload =
      'event: node\ndata: {"node":"classify"}\n\n' +
      'event: done\ndata: {"outcome":"answer","summary":"ok","sql":null,"result":null,"error":null}\n\n';
    mockFetch.mockResolvedValue(
      new Response(sseStream(payload), {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      }),
    );

    const res = await post({ question: "q" });
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toBe("text/event-stream");
    const text = await res.text();
    expect(text).toBe(payload);
  });
});
