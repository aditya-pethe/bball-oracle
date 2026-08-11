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

// Mirrored exactly in agent/context.py, which re-clamps on arrival — bounds that
// only the producer applies are not bounds. Raised from 8/8000 on 2026-08-11:
// eight messages was measured too tight on a real thread, where one answered
// question followed by seven clarifications pushed the only answer out of the
// window. The byte ceiling scales with the message cap so that raising one does
// not leave the other silently binding first.
export const MAX_CONTEXT_MESSAGES = 16;
export const MAX_PREVIEW_ROWS = 5;
export const MAX_SERIALIZED_BYTES = 16000;

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

/** Indices of (question, answer) for the most recent successfully answered
 * exchange — the thing a follow-up needs and the drafter transforms. */
function lastAnsweredPair(turns: ConversationTurn[]): [number, number] | null {
  for (let i = turns.length - 1; i >= 0; i--) {
    if (turns[i].outcome !== "answer" || !turns[i].sql) continue;
    for (let j = i - 1; j >= 0; j--) if (turns[j].role === "user") return [j, i];
    return null;
  }
  return null;
}

/**
 * Turns that survive trimming regardless of age. Both are exchanges rather than
 * single turns, because half an exchange resumes nothing:
 *
 * - the most recent ANSWERED exchange. Trimming used to count messages, which
 *   valued a content-free "I need clarification" turn exactly as highly as an
 *   answer carrying SQL and results. Measured on a real thread on 2026-08-11: one
 *   answered question followed by seven clarifications evicted the only answer,
 *   leaving the model eight messages of its own clarification requests.
 * - an unresolved clarification, for the reason it always was: without the
 *   original question, "you asked the user to clarify" is not a resumable task.
 */
function protectedIndices(turns: ConversationTurn[]): Set<number> {
  const keep = new Set<number>();
  const pending = pendingPair(turns);
  if (pending !== null) pending.forEach((i) => keep.add(i));
  const answered = lastAnsweredPair(turns);
  if (answered !== null) answered.forEach((i) => keep.add(i));
  return keep;
}

function clampCount(turns: ConversationTurn[]): ConversationTurn[] {
  if (turns.length <= MAX_CONTEXT_MESSAGES) return turns;

  // Protected turns first, then fill the remaining budget from the most recent end
  // backwards. Recency still governs everything unprotected — what "that" or "him"
  // refers to is almost always the previous turn.
  const keep = protectedIndices(turns);
  let budget = MAX_CONTEXT_MESSAGES - keep.size;
  for (let i = turns.length - 1; i >= 0 && budget > 0; i--) {
    if (keep.has(i)) continue;
    keep.add(i);
    budget--;
  }

  // Sorted, because the kept turns are spliced from two regions of the thread and a
  // conversation rendered out of order is worse than a shorter one.
  return [...keep].sort((a, b) => a - b).map((i) => turns[i]);
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
 * go first and protected ones go last — otherwise this stage would silently undo
 * clampCount's work the moment a few turns carried long SQL, dropping the answered
 * exchange it had just gone out of its way to keep. Protection is recomputed each
 * pass because indices shift as turns leave.
 *
 * The last turn is never dropped, because an empty context reads to the model as
 * "no history" — a different, and wrong, statement than "history was too big to
 * send".
 */
function fitBytes(turns: ConversationTurn[]): ConversationTurn[] {
  let out = turns;
  while (out.length > 1 && size(out) > MAX_SERIALIZED_BYTES) {
    const keep = protectedIndices(out);
    let victim = out.findIndex((_, i) => !keep.has(i));
    if (victim === -1) victim = 0;
    out = out.filter((_, i) => i !== victim);
  }
  for (let guard = 0; out.length > 0 && size(out) > MAX_SERIALIZED_BYTES && guard < 64; guard++) {
    out = out.map(shrink);
  }
  return out;
}

/** Persisted turns (oldest first) → the bounded payload the service receives. */
export function buildConversationContext(messages: AgentMessage[]): ConversationContext {
  return { turns: fitBytes(clampCount(messages.map(toTurn))) };
}
