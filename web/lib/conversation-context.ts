import type { AgentMessage, AgentOutcome, QueryResult } from "./api-types";

/**
 * The TypeScript half of the conversation-context contract
 * (.agents/p5_conversational_context.md). `agent/context.py` is the other half and
 * the two must agree field for field — hence the snake_case wire names, which are
 * what the Pydantic model expects.
 *
 * Next.js is the authority for conversation ownership and persistence. It reads a
 * bounded window of the thread and sends it alongside the question; the Python
 * service never fetches a conversation and still holds no database credential.
 *
 * This module is the projection and the bounds only — the ownership-scoped read is
 * `readConversationContext` in conversations.ts, and the prompt rendering is
 * agent/context.py. What deliberately never crosses this boundary: `durationMs`,
 * node timings, token counts, and full result sets.
 */

// Initial bounds, to be validated against multi-turn eval latency/token numbers.
// Mirrored exactly in agent/context.py, which re-clamps on arrival — bounds that
// only the producer applies are not bounds.
export const MAX_CONTEXT_MESSAGES = 8;
export const MAX_PREVIEW_ROWS = 5;
export const MAX_SERIALIZED_BYTES = 8000;

export type ContextResultShape = {
  columns: string[];
  row_count: number;
  truncated: boolean;
};

export type ConversationTurn = {
  role: "user" | "assistant";
  text: string;
  outcome?: AgentOutcome;
  sql?: string;
  result_shape?: ContextResultShape;
  result_preview?: unknown[][];
};

export type ConversationContext = { turns: ConversationTurn[] };

export type PendingClarification = {
  originalQuestion: string;
  clarifyQuestion: string;
};

function okOrNull(result: QueryResult | null): Extract<QueryResult, { status: "ok" }> | null {
  return result !== null && result.status === "ok" ? result : null;
}

function toTurn(message: AgentMessage): ConversationTurn {
  if (message.role === "user") {
    return { role: "user", text: message.text };
  }

  const { outcome, summary, sql, error } = message.envelope;
  const ok = okOrNull(message.envelope.result);
  // A turn is "failed" when it errored OR when its query did not return rows —
  // either way its SQL is not a base a follow-up should be transformed from.
  const failed = error !== null || (sql !== null && ok === null && outcome === "answer");

  const turn: ConversationTurn = {
    role: "assistant",
    text: failed && error ? `${summary} (failed: ${error})`.trim() : summary,
    outcome,
  };
  if (!failed && sql !== null) turn.sql = sql;
  if (!failed && ok !== null) {
    turn.result_shape = {
      columns: ok.columns,
      row_count: ok.rowCount,
      truncated: ok.truncated,
    };
    turn.result_preview = ok.rows.slice(0, MAX_PREVIEW_ROWS);
  }
  return turn;
}

function lastAssistantIndex(turns: ConversationTurn[]): number {
  for (let i = turns.length - 1; i >= 0; i--) if (turns[i].role === "assistant") return i;
  return -1;
}

/**
 * An unresolved clarification, or null.
 *
 * Message order is the source of truth — there is no mutable "resolved" column.
 * The latest assistant turn having asked for clarification IS the pending state,
 * because any later answer or decline would be the latest turn instead.
 */
export function pendingClarification(context: ConversationContext): PendingClarification | null {
  const index = lastAssistantIndex(context.turns);
  if (index === -1 || context.turns[index].outcome !== "clarify") return null;
  for (let i = index - 1; i >= 0; i--) {
    if (context.turns[i].role === "user") {
      return {
        originalQuestion: context.turns[i].text,
        clarifyQuestion: context.turns[index].text,
      };
    }
  }
  return null;
}

/** Indices of (original question, clarifying question) for an unresolved
 * clarification, so the count bound can pin them. */
function pendingPair(turns: ConversationTurn[]): [number, number] | null {
  const index = lastAssistantIndex(turns);
  if (index === -1 || turns[index].outcome !== "clarify") return null;
  for (let i = index - 1; i >= 0; i--) if (turns[i].role === "user") return [i, index];
  return null;
}

function clampCount(turns: ConversationTurn[]): ConversationTurn[] {
  if (turns.length <= MAX_CONTEXT_MESSAGES) return turns;

  const pair = pendingPair(turns);
  const windowStart = turns.length - MAX_CONTEXT_MESSAGES;
  if (pair === null || pair[0] >= windowStart) return turns.slice(-MAX_CONTEXT_MESSAGES);

  // An unresolved clarification is retained even when the recent-window rule
  // would drop it: without the original question there is nothing to resume.
  //
  // The pinned indices are excluded from the tail rather than assumed to be outside
  // it. Only pair[0] is known to fall before the window; pair[1] is the LAST
  // assistant turn, so with a long run of user turns after it, it can sit inside the
  // tail and be rendered twice.
  const keep = MAX_CONTEXT_MESSAGES - 2;
  const tailStart = turns.length - keep;
  const tail = turns.filter((_, i) => i >= tailStart && i !== pair[0] && i !== pair[1]);
  return [turns[pair[0]], turns[pair[1]], ...tail].slice(0, MAX_CONTEXT_MESSAGES);
}

function size(turns: ConversationTurn[]): number {
  return JSON.stringify({ turns }).length;
}

function halve(value: string): string {
  return value.length > 1 ? value.slice(0, Math.floor(value.length / 2)) : value;
}

function shrink(turn: ConversationTurn): ConversationTurn {
  const next: ConversationTurn = { ...turn, text: halve(turn.text) };
  if (next.sql !== undefined) next.sql = halve(next.sql);
  if (next.result_preview !== undefined) {
    next.result_preview = next.result_preview.map((row) =>
      row.map((cell) => (typeof cell === "string" ? halve(cell) : cell)),
    );
  }
  return next;
}

/**
 * The hard serialized-size ceiling, enforced before the service call. Oldest turns
 * go first; the last turn is never dropped, because an empty context reads to the
 * model as "no history" — a different, and wrong, statement than "history was too
 * big to send".
 */
function fitBytes(turns: ConversationTurn[]): ConversationTurn[] {
  let out = turns;
  while (out.length > 1 && size(out) > MAX_SERIALIZED_BYTES) out = out.slice(1);
  for (let guard = 0; out.length > 0 && size(out) > MAX_SERIALIZED_BYTES && guard < 64; guard++) {
    out = out.map(shrink);
  }
  return out;
}

/** Persisted turns (oldest first) → the bounded payload the service receives. */
export function buildConversationContext(messages: AgentMessage[]): ConversationContext {
  return { turns: fitBytes(clampCount(messages.map(toTurn))) };
}
