import { requireSession } from "../../../lib/require-session";

/**
 * `/api/agent` — a THIN authenticating proxy to the Python agent service
 * (.agents/p4_agent.md "Architecture" / "Service split"). `requireSession()` is the
 * only auth gate; the Python service never parses a session, so a caller with no
 * cookie has no way in. `session.userId` is substituted for whatever (if anything)
 * the browser body claims — the point of a proxy that runs SQL on someone's behalf
 * is that the someone is never taken on faith.
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

/**
 * Conversation ids are opaque client-generated strings this step (no server-side
 * conversation persistence yet — migration 0004 is unapplied, see AGENTS.md status).
 * `agent/service.py`'s `AgentRequest.conversation_id: str | None` already expects a
 * string, so this stays a passthrough with no numeric coercion to invent.
 */
function readConversationId(body: unknown): string | null {
  const raw = (body as { conversationId?: unknown })?.conversationId;
  return typeof raw === "string" && raw.trim() !== "" ? raw : null;
}

export async function POST(req: Request) {
  const session = await requireSession();
  if (!session) {
    return Response.json({ error: "authentication required" }, { status: 401 });
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
  const conversationId = readConversationId(body);

  const baseUrl = process.env.AGENT_SERVICE_URL;
  const token = process.env.AGENT_SERVICE_TOKEN;
  if (!baseUrl || !token) {
    return Response.json({ error: "agent service is not configured" }, { status: 500 });
  }

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
        conversation_id: conversationId,
      }),
      signal: req.signal,
    });
  } catch (err) {
    return Response.json(
      {
        error: `agent service unreachable: ${err instanceof Error ? err.message : String(err)}`,
      },
      { status: 502 },
    );
  }

  if (!upstream.ok || !upstream.body) {
    const text = await upstream.text().catch(() => "");
    const status = upstream.status >= 400 && upstream.status < 600 ? upstream.status : 502;
    return Response.json({ error: text || `agent service returned ${upstream.status}` }, { status });
  }

  return new Response(upstream.body, {
    status: 200,
    headers: {
      "content-type": "text/event-stream",
      "cache-control": "no-cache, no-transform",
      connection: "keep-alive",
      // Belt-and-suspenders for any buffering reverse proxy in front of this route
      // (web/node_modules/next/dist/docs streaming guide, "Reverse proxies").
      "x-accel-buffering": "no",
    },
  });
}
