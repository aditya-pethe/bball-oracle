// The client-side contract for every API response. Routes and components both import
// from here; components consume these shapes as-is and never reshape them.

export type OkResult = {
  status: "ok";
  columns: string[];
  rows: unknown[][];
  rowCount: number;
  truncated: boolean;
  durationMs: number;
};

// The four non-200 outcomes of POST /api/query, discriminated for rendering. The page's
// run flow builds these from (HTTP status, body): 429 -> rate_limited, 504 -> timeout,
// 400 with durationMs -> SQL error, 400 without -> validation rejection.
export type QueryFailure =
  | { status: "validation_rejected"; error: string }
  | { status: "error"; error: string; durationMs: number }
  | { status: "timeout"; error: string; durationMs: number }
  | { status: "rate_limited"; error: string; retryAfterSeconds: number };

export type QueryResult = OkResult | QueryFailure;

export type QueryUiState = "idle" | "running" | QueryResult;

export type SchemaColumn = { name: string; type: string };
export type SchemaTable = { name: string; columns: SchemaColumn[] };
export type SchemaResponse = { tables: SchemaTable[] };

export type HistoryStatus = "ok" | "validation_rejected" | "timeout" | "error";
export type HistoryEntry = {
  sql: string;
  status: HistoryStatus;
  startedAt: string;
  durationMs: number | null;
  rowCount: number | null;
  truncated: boolean;
};
export type HistoryResponse = { queries: HistoryEntry[] };

// ---------------------------------------------------------------------------
// Agent tab (Phase 4). These mirror evals/harness/envelope.py field for field -- the same
// envelope is what the UI renders and what the eval harness scores, so the shape is defined
// once and both consumers agree by construction. Changing one without the other is a bug.
// ---------------------------------------------------------------------------

/**
 * Deliberately independent of whether `sql` is present: an agent that asks a clarifying
 * question *and* emits speculative SQL has still clarified, and one that declines while
 * quietly returning rows has not.
 */
export type AgentOutcome = "answer" | "clarify" | "decline";

export type AgentEnvelope = {
  outcome: AgentOutcome;
  summary: string;
  /** Always disclosed, never hidden — the agent drafts SQL, the user stays the judge. */
  sql: string | null;
  /**
   * QueryResult is the universal result contract: the same shape /api/query returns and the
   * same one ResultsArea/TableView already render, failures included.
   *
   * When persisted into app.conversation_message this is a PREVIEW (<= 50 rows), never the
   * full 1000-row result — see db/migrations/0004. Full results are re-executed on demand.
   */
  result: QueryResult | null;
  error: string | null;
};

export type Conversation = {
  id: number;
  title: string | null;
  createdAt: string;
  updatedAt: string;
};

/** One turn. Discriminated on `role` because the two roles carry different payloads. */
export type AgentMessage =
  | { id: number; role: "user"; createdAt: string; text: string }
  | { id: number; role: "assistant"; createdAt: string; envelope: AgentEnvelope };

export type ConversationListResponse = { conversations: Conversation[] };
export type ConversationResponse = { conversation: Conversation; messages: AgentMessage[] };

/**
 * `GET /api/agent` — whether asking anything is possible right now, so the tab can degrade
 * before the user types rather than after. `enabled` is the AGENT_ENABLED kill switch;
 * `remaining` is what is left of the per-user daily message budget.
 */
export type AgentStatusResponse = {
  enabled: boolean;
  used: number;
  limit: number;
  remaining: number;
};
