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
