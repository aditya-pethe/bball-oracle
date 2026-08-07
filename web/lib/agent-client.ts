import type { AgentEnvelope, AgentOutcome, OkResult, QueryResult } from "./api-types";

/**
 * The wire shape of the `done` SSE event, exactly as `agent/service.py`'s
 * `_envelope()` builds it (confirmed by reading that function, not guessed). It
 * differs from api-types.ts's `AgentEnvelope` in one place: `result` carries only
 * `{columns, rows, rowCount, truncated}` — no `status`, no `durationMs`. The agent
 * loop never measures a single "this is how long the query took" number the way
 * `/api/query` does (a turn can retry the query several times), so this module
 * adapts the wire shape into a real `QueryResult` at the boundary instead of
 * widening the shared type for one caller.
 */
export type RawAgentResult = {
  columns: string[];
  rows: unknown[][];
  rowCount: number;
  truncated: boolean;
};

export type RawAgentEnvelope = {
  outcome: AgentOutcome;
  summary: string;
  sql: string | null;
  result: RawAgentResult | null;
  error: string | null;
};

/** One `node` SSE event: a graph node just finished. */
export type AgentNodeEvent = {
  type: "node";
  node: string;
  durationMs: number | null;
};

/** The terminal event: the graph reached an outcome. */
export type AgentDoneEvent = {
  type: "done";
  envelope: AgentEnvelope;
};

export type AgentStreamEvent = AgentNodeEvent | AgentDoneEvent;

/** Thrown for a non-streaming failure: the proxy rejected the request, the service
 * is unreachable, or the connection dropped mid-stream. `status` is the HTTP status
 * of `/api/agent`'s own response, when there was one (absent for a network error). */
export class AgentClientError extends Error {
  status?: number;

  constructor(message: string, status?: number) {
    super(message);
    this.name = "AgentClientError";
    this.status = status;
  }
}

/**
 * Turns the wire envelope's `result`/`error` into the same `QueryResult` shape
 * `ResultsArea`/`TableView` already render, so the agent tab is pure reuse — no
 * second result renderer. `elapsedMs` is measured client-side (time since the
 * question was sent) since the wire envelope carries no query-only duration to
 * attribute it to; it is a "time to this answer" figure, not a raw SQL time.
 */
export function toQueryResult(raw: RawAgentEnvelope, elapsedMs: number): QueryResult | null {
  if (raw.result) {
    const ok: OkResult = {
      status: "ok",
      columns: raw.result.columns,
      rows: raw.result.rows,
      rowCount: raw.result.rowCount,
      truncated: raw.result.truncated,
      durationMs: elapsedMs,
    };
    return ok;
  }
  if (raw.error) {
    return { status: "error", error: raw.error, durationMs: elapsedMs };
  }
  return null;
}

export function toAgentEnvelope(raw: RawAgentEnvelope, elapsedMs: number): AgentEnvelope {
  return {
    outcome: raw.outcome,
    summary: raw.summary,
    sql: raw.sql,
    result: toQueryResult(raw, elapsedMs),
    error: raw.error,
  };
}

function parseSseBlock(raw: string): { event: string; data: string } | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) event = line.slice("event:".length).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice("data:".length).trim());
  }
  if (dataLines.length === 0) return null;
  return { event, data: dataLines.join("\n") };
}

export type AgentStreamParams = {
  question: string;
  /** Opaque client-generated thread id, or null for a fresh one. See route.ts —
   * conversation persistence doesn't exist yet, so this is a passthrough only. */
  conversationId?: string | null;
  signal?: AbortSignal;
};

/**
 * POSTs to `/api/agent` and yields each SSE event as it arrives: per-node progress,
 * then a terminal `done` carrying the envelope. Throws `AgentClientError` for a
 * non-streaming failure (auth, misconfiguration, the service being down) — callers
 * should not see those as just another envelope outcome.
 */
export async function* streamAgentAnswer(
  params: AgentStreamParams,
): AsyncGenerator<AgentStreamEvent> {
  const startedAt = Date.now();
  let res: Response;
  try {
    res = await fetch("/api/agent", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        question: params.question,
        conversationId: params.conversationId ?? null,
      }),
      signal: params.signal,
    });
  } catch (err) {
    throw new AgentClientError(
      `network error: ${err instanceof Error ? err.message : String(err)}`,
    );
  }

  if (!res.ok || !res.body) {
    const body = (await res.json().catch(() => ({}))) as Record<string, unknown>;
    const message = typeof body.error === "string" ? body.error : `agent request failed (${res.status})`;
    throw new AgentClientError(message, res.status);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let boundary: number;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const block = parseSseBlock(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      if (!block) continue;

      if (block.event === "node") {
        const data = JSON.parse(block.data) as {
          node: string;
          node_timings?: { node: string; duration_ms: number }[];
        };
        const timing = data.node_timings?.find((t) => t.node === data.node);
        yield { type: "node", node: data.node, durationMs: timing ? timing.duration_ms : null };
      } else if (block.event === "done") {
        const raw = JSON.parse(block.data) as RawAgentEnvelope;
        yield { type: "done", envelope: toAgentEnvelope(raw, Date.now() - startedAt) };
      }
    }
  }
}

/** Human labels for the graph nodes (agent/graph.py), for the per-node progress list. */
export const NODE_LABELS: Record<string, string> = {
  classify: "Classifying question",
  clarify: "Clarifying",
  decline: "Declining",
  draft_sql: "Drafting SQL",
  execute: "Running query",
  critic: "Reviewing result",
  summarize: "Summarizing",
};
