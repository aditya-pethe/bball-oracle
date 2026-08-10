import { requireSession } from "../../../lib/require-session";
import { getAppPool } from "../../../lib/execute-query";
import { agentEnabled, checkAgentBudget } from "../../../lib/agent-budget";
import { toAgentEnvelope, type RawAgentEnvelope } from "../../../lib/agent-client";
import {
  appendAssistantPlaceholder,
  appendUserMessage,
  createConversation,
  finalizeAssistantMessage,
  readConversationId,
  titleFrom,
  touchConversation,
} from "../../../lib/conversations";
import type { AgentEnvelope } from "../../../lib/api-types";

/**
 * `/api/agent` — a THIN authenticating proxy to the Python agent service
 * (.agents/p4_agent.md "Architecture" / "Service split"). `requireSession()` is the
 * only auth gate; the Python service never parses a session, so a caller with no
 * cookie has no way in. `session.userId` is substituted for whatever (if anything)
 * the browser body claims — the point of a proxy that runs SQL on someone's behalf
 * is that the someone is never taken on faith.
 *
 * The gates run in a deliberate order and every one of them is before the upstream call:
 *   session -> kill switch -> body -> daily budget -> service config -> persist -> proxy.
 * The budget in particular MUST precede `fetch`, because the cost being capped is the model
 * call that `fetch` starts. `checkRateLimit` cannot do this job: it counts `app.query_log`
 * rows, and a `clarify`/`decline` turn spends a model call while executing no SQL at all.
 *
 * The loop this forwards into can span several model turns plus queries
 * (.agents/p4_agent.md "Streaming + Vercel constraints"), so the response is piped
 * straight through as it arrives rather than buffered — a buffered response would
 * feel dead and risk a platform timeout either way.
 */

// Generous ceiling for the correction ladder's worst case (bounded retry, several
// model turns, a query each) — this is a proxy hop, not the loop itself, so the real
// budget is enforced by agent/graph.py's RetryLimits, not this number.
export const maxDuration = 60;

function readQuestion(body: unknown): string | null {
  const question = (body as { question?: unknown })?.question;
  return typeof question === "string" && question.trim() !== "" ? question : null;
}

/** The envelope written when the stream ends without a `done` event (service crash, dropped
 * connection). Persisting it is what stops a half-finished turn from reading as an empty
 * answer when the thread is reloaded. */
function interruptedEnvelope(reason: string): AgentEnvelope {
  return { outcome: "answer", summary: "", sql: null, result: null, error: reason };
}

/**
 * Passes the upstream SSE bytes through untouched while reading the terminal `done` event out
 * of them, so the assistant turn can be persisted without buffering the response.
 *
 * The write happens in `flush`, which runs before the response stream closes — i.e. still
 * inside the request's lifetime, which a fire-and-forget promise after `return` would not be
 * on a serverless platform.
 */
function captureEnvelope(messageId: number, startedAt: number): TransformStream<Uint8Array, Uint8Array> {
  const decoder = new TextDecoder();
  let buffer = "";
  let envelope: AgentEnvelope | null = null;

  const consume = (block: string) => {
    let event = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice("event:".length).trim();
      else if (line.startsWith("data:")) data.push(line.slice("data:".length).trim());
    }
    if (event !== "done" || data.length === 0) return;
    try {
      const raw = JSON.parse(data.join("\n")) as RawAgentEnvelope;
      envelope = toAgentEnvelope(raw, Date.now() - startedAt);
    } catch {
      // A malformed terminal event is a service bug, not a reason to fail the user's
      // response — the turn is persisted as interrupted below.
    }
  };

  return new TransformStream({
    transform(chunk, controller) {
      controller.enqueue(chunk);
      buffer += decoder.decode(chunk, { stream: true });
      let boundary: number;
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        consume(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + 2);
      }
    },
    async flush() {
      if (buffer.trim() !== "") consume(buffer);
      await finalizeAssistantMessage(
        messageId,
        envelope ?? interruptedEnvelope("the agent service closed the stream without an answer"),
      );
    },
  });
}

/**
 * Whether the agent can be asked anything right now: the kill switch, plus what is left of
 * today's per-user budget. The tab reads this on mount so a disabled agent or an exhausted
 * budget shows as a notice instead of as a failed question.
 */
export async function GET() {
  const session = await requireSession();
  if (!session) {
    return Response.json({ error: "authentication required" }, { status: 401 });
  }

  if (!agentEnabled()) {
    return Response.json({ enabled: false, used: 0, limit: 0, remaining: 0 });
  }

  const budget = await checkAgentBudget(getAppPool(), session.userId);
  return Response.json({
    enabled: true,
    used: budget.used,
    limit: budget.limit,
    remaining: Math.max(0, budget.limit - budget.used),
  });
}

export async function POST(req: Request) {
  const session = await requireSession();
  if (!session) {
    return Response.json({ error: "authentication required" }, { status: 401 });
  }

  if (!agentEnabled()) {
    return Response.json(
      { error: "the agent is currently disabled", disabled: true },
      { status: 503 },
    );
  }

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return Response.json(
      { error: "request body must be JSON: { \"question\": \"...\" }" },
      { status: 400 },
    );
  }

  const question = readQuestion(body);
  if (question === null) {
    return Response.json({ error: "missing question" }, { status: 400 });
  }

  const budget = await checkAgentBudget(getAppPool(), session.userId);
  if (!budget.allowed) {
    return Response.json(
      {
        error: `daily agent limit reached (${budget.used}/${budget.limit} messages) — resets in ${Math.ceil(budget.retryAfterSeconds / 60)} min`,
        retryAfterSeconds: budget.retryAfterSeconds,
        used: budget.used,
        limit: budget.limit,
      },
      { status: 429, headers: { "Retry-After": String(budget.retryAfterSeconds) } },
    );
  }

  const baseUrl = process.env.AGENT_SERVICE_URL;
  const token = process.env.AGENT_SERVICE_TOKEN;
  if (!baseUrl || !token) {
    return Response.json({ error: "agent service is not configured" }, { status: 500 });
  }

  // Persistence happens after every rejection path above, so a refused request never leaves
  // rows behind — and before the upstream call, because query_log.conversation_message_id
  // needs the assistant row to exist by the time the agent executes SQL.
  const requested = readConversationId((body as { conversationId?: unknown })?.conversationId);
  const conversationId =
    requested ?? (await createConversation(session.userId, titleFrom(question))).id;

  const userMessageId = await appendUserMessage(session.userId, conversationId, question);
  if (userMessageId === null) {
    return Response.json({ error: "conversation not found" }, { status: 404 });
  }
  const assistantMessageId = await appendAssistantPlaceholder(session.userId, conversationId);
  if (assistantMessageId === null) {
    return Response.json({ error: "conversation not found" }, { status: 404 });
  }
  await touchConversation(conversationId, titleFrom(question));

  const startedAt = Date.now();
  let upstream: Response;
  try {
    upstream = await fetch(`${baseUrl.replace(/\/+$/, "")}/agent`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        // The shared secret lives only in this outgoing header — it is an env var
        // read server-side and never echoed into anything sent back to the browser.
        authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({
        question,
        user_id: session.userId,
        conversation_id: String(conversationId),
        // Forwarded so the service can hand it to /api/internal/query, which writes it to
        // query_log.conversation_message_id. As of this change agent/service.py does NOT read
        // this field and agent/execute.py does NOT forward it, so the column still lands NULL —
        // agent/ is out of scope for this branch. See the report.
        conversation_message_id: assistantMessageId,
      }),
      signal: req.signal,
    });
  } catch (err) {
    const message = `agent service unreachable: ${err instanceof Error ? err.message : String(err)}`;
    await finalizeAssistantMessage(assistantMessageId, interruptedEnvelope(message));
    return Response.json({ error: message }, { status: 502 });
  }

  if (!upstream.ok || !upstream.body) {
    const text = await upstream.text().catch(() => "");
    const status = upstream.status >= 400 && upstream.status < 600 ? upstream.status : 502;
    const message = text || `agent service returned ${upstream.status}`;
    await finalizeAssistantMessage(assistantMessageId, interruptedEnvelope(message));
    return Response.json({ error: message }, { status });
  }

  return new Response(upstream.body.pipeThrough(captureEnvelope(assistantMessageId, startedAt)), {
    status: 200,
    headers: {
      "content-type": "text/event-stream",
      "cache-control": "no-cache, no-transform",
      connection: "keep-alive",
      // Belt-and-suspenders for any buffering reverse proxy in front of this route
      // (web/node_modules/next/dist/docs streaming guide, "Reverse proxies").
      "x-accel-buffering": "no",
      // The ids the browser needs to keep talking to this thread. Headers rather than a
      // synthetic SSE event so the transport stays byte-for-byte what the service produced.
      "x-conversation-id": String(conversationId),
      "x-conversation-message-id": String(assistantMessageId),
      "access-control-expose-headers": "x-conversation-id, x-conversation-message-id",
    },
  });
}
