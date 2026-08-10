import type { AgentEnvelope, AgentMessage, Conversation, QueryResult } from "./api-types";
import { getAppPool } from "./execute-query";

/**
 * Persistence for the agent tab's threads (`app.conversation` / `app.conversation_message`,
 * db/migrations/0004).
 *
 * Every function here takes the caller's verified `userId` and scopes on it in SQL. There is
 * no "fetch by id, then check the owner in JavaScript" path, because that is the shape where
 * an early return eventually goes missing. Same ownership pattern as
 * `web/app/api/history/route.ts`: rows are filtered by `user_id`, never merely audited after.
 */

/**
 * STORAGE CONSTRAINT, from 0004's own column comment and non-negotiable: `content` never holds
 * a full QueryResult. The executor's cap is 1000 rows; a 40-turn thread of thousand-row JSONB
 * payloads would outgrow the 216MB NBA dataset on the Supabase free tier. An assistant turn
 * keeps the SQL, the query_log linkage (which lives on query_log, not here) and at most this
 * many preview rows; the full result is re-executed on demand.
 */
export const MAX_PREVIEW_ROWS = 50;

export const MAX_TITLE_LENGTH = 60;

export function titleFrom(question: string): string {
  const trimmed = question.trim().replace(/\s+/g, " ");
  return trimmed.length > MAX_TITLE_LENGTH
    ? `${trimmed.slice(0, MAX_TITLE_LENGTH - 1)}…`
    : trimmed;
}

/** Drops everything past MAX_PREVIEW_ROWS and marks the result truncated, so a reader of a
 * persisted turn can tell a 50-row answer from a 50-row window onto a larger one. */
export function toPreviewResult(result: QueryResult | null): QueryResult | null {
  if (result === null || result.status !== "ok") return result;
  if (result.rows.length <= MAX_PREVIEW_ROWS) return result;
  return {
    ...result,
    rows: result.rows.slice(0, MAX_PREVIEW_ROWS),
    truncated: true,
  };
}

export function toStoredEnvelope(envelope: AgentEnvelope): AgentEnvelope {
  return { ...envelope, result: toPreviewResult(envelope.result) };
}

/** What an assistant row holds between "the turn started" and "the envelope arrived". A turn
 * whose stream died keeps it forever, which is why reads map it to a visible error rather
 * than to an empty answer. */
const PENDING_CONTENT = { pending: true } as const;

const INTERRUPTED: AgentEnvelope = {
  outcome: "answer",
  summary: "",
  sql: null,
  result: null,
  error: "this answer was interrupted before the agent finished",
};

type ConversationRow = {
  id: string | number;
  title: string | null;
  created_at: Date;
  updated_at: Date;
};

type MessageRow = {
  id: string | number;
  role: string;
  content: unknown;
  created_at: Date;
};

function toConversation(row: ConversationRow): Conversation {
  return {
    id: Number(row.id),
    title: row.title,
    createdAt: row.created_at.toISOString(),
    updatedAt: row.updated_at.toISOString(),
  };
}

function toMessage(row: MessageRow): AgentMessage {
  const id = Number(row.id);
  const createdAt = row.created_at.toISOString();
  if (row.role === "user") {
    const text = (row.content as { text?: unknown })?.text;
    return { id, role: "user", createdAt, text: typeof text === "string" ? text : "" };
  }
  const content = row.content as Record<string, unknown> | null;
  const envelope =
    content && content.pending === true ? INTERRUPTED : (content as unknown as AgentEnvelope);
  return { id, role: "assistant", createdAt, envelope: envelope ?? INTERRUPTED };
}

/** `id` is a bigint identity, which node-pg hands back as a string; every id crossing this
 * boundary is normalised to a number so the API contract in api-types.ts stays honest. */
function toId(raw: unknown): number | null {
  if (typeof raw === "number") return Number.isSafeInteger(raw) && raw > 0 ? raw : null;
  if (typeof raw === "string" && /^[1-9]\d*$/.test(raw)) {
    const parsed = Number(raw);
    return Number.isSafeInteger(parsed) ? parsed : null;
  }
  return null;
}

export function readConversationId(raw: unknown): number | null {
  return raw === undefined || raw === null ? null : toId(raw);
}

export async function listConversations(userId: number): Promise<Conversation[]> {
  const { rows } = await getAppPool().query<ConversationRow>(
    `SELECT id, title, created_at, updated_at
       FROM app.conversation
      WHERE user_id = $1
      ORDER BY updated_at DESC, id DESC
      LIMIT 50`,
    [userId],
  );
  return rows.map(toConversation);
}

export async function createConversation(
  userId: number,
  title: string | null,
): Promise<Conversation> {
  const { rows } = await getAppPool().query<ConversationRow>(
    `INSERT INTO app.conversation (user_id, title)
     VALUES ($1, $2)
     RETURNING id, title, created_at, updated_at`,
    [userId, title],
  );
  return toConversation(rows[0]);
}

export async function getConversation(
  userId: number,
  id: number,
): Promise<{ conversation: Conversation; messages: AgentMessage[] } | null> {
  const pool = getAppPool();
  const { rows } = await pool.query<ConversationRow>(
    `SELECT id, title, created_at, updated_at
       FROM app.conversation
      WHERE id = $1 AND user_id = $2`,
    [id, userId],
  );
  if (rows.length === 0) return null;

  const messages = await pool.query<MessageRow>(
    `SELECT id, role, content, created_at
       FROM app.conversation_message
      WHERE conversation_id = $1
      ORDER BY id`,
    [id],
  );
  return { conversation: toConversation(rows[0]), messages: messages.rows.map(toMessage) };
}

export async function deleteConversation(userId: number, id: number): Promise<boolean> {
  const { rows } = await getAppPool().query(
    `DELETE FROM app.conversation WHERE id = $1 AND user_id = $2 RETURNING id`,
    [id, userId],
  );
  return rows.length === 1;
}

/**
 * Appends a turn, but only into a thread this user owns — the ownership check is the INSERT's
 * own WHERE EXISTS, so there is no window between checking and writing. Returns null when the
 * conversation does not exist or belongs to someone else; the caller renders that as a 404.
 */
async function appendMessage(
  userId: number,
  conversationId: number,
  role: "user" | "assistant",
  content: unknown,
): Promise<number | null> {
  const { rows } = await getAppPool().query(
    `INSERT INTO app.conversation_message (conversation_id, role, content)
     SELECT $1, $2, $3::jsonb
      WHERE EXISTS (SELECT 1 FROM app.conversation WHERE id = $1 AND user_id = $4)
     RETURNING id`,
    [conversationId, role, JSON.stringify(content), userId],
  );
  return rows.length === 1 ? toId(rows[0].id) : null;
}

export function appendUserMessage(
  userId: number,
  conversationId: number,
  text: string,
): Promise<number | null> {
  return appendMessage(userId, conversationId, "user", { text });
}

/**
 * The assistant row is written BEFORE the agent runs, because its id is what
 * `query_log.conversation_message_id` points at: the audit link has to exist by the time the
 * agent executes SQL, not after it finishes. `finalizeAssistantMessage` fills in the envelope.
 */
export function appendAssistantPlaceholder(
  userId: number,
  conversationId: number,
): Promise<number | null> {
  return appendMessage(userId, conversationId, "assistant", PENDING_CONTENT);
}

export async function finalizeAssistantMessage(
  messageId: number,
  envelope: AgentEnvelope,
): Promise<void> {
  await getAppPool().query(
    `UPDATE app.conversation_message SET content = $2::jsonb WHERE id = $1`,
    [messageId, JSON.stringify(toStoredEnvelope(envelope))],
  );
}

/** 0004 denormalises `updated_at` so the left rail can order threads without touching the
 * message table; nothing else keeps it current. Titles a thread on its first question. */
export async function touchConversation(
  conversationId: number,
  titleIfUnset: string | null = null,
): Promise<void> {
  await getAppPool().query(
    `UPDATE app.conversation
        SET updated_at = now(),
            title = COALESCE(title, $2)
      WHERE id = $1`,
    [conversationId, titleIfUnset],
  );
}
