import { Pool } from "pg";
import Cursor from "pg-cursor";

export type ExecuteResult =
  | {
      status: "ok";
      rows: unknown[][];
      columns: string[];
      rowCount: number;
      truncated: boolean;
      durationMs: number;
    }
  | { status: "timeout"; durationMs: number }
  | { status: "error"; message: string; durationMs: number };

export type QueryLogStatus = "ok" | "validation_rejected" | "timeout" | "error";

export type QueryLogEntry = {
  userId: number;
  clientIp: string | null;
  queryText: string;
  status: QueryLogStatus;
  durationMs?: number | null;
  rowCount?: number | null;
  truncated?: boolean;
  errorText?: string | null;
};

const DEFAULT_TIMEOUT_MS = 5000;
const DEFAULT_MAX_ROWS = 1000;
const QUERY_CANCELED = "57014";

let sandboxPool: Pool | null = null;
let appPool: Pool | null = null;

function createPool(envVar: string): Pool {
  const connectionString = process.env[envVar];
  if (!connectionString) throw new Error(`${envVar} is not set`);

  const pool = new Pool({ connectionString, max: 5 });
  // node-pg re-emits idle-client failures on the pool, and an unhandled 'error' event takes
  // the process down. The pool discards the broken client either way.
  pool.on("error", () => {});
  return pool;
}

/** User SQL only. sandbox_ro cannot see the `app` schema, so nothing is ever logged through it. */
export function getSandboxPool(): Pool {
  sandboxPool ??= createPool("SANDBOX_RO_DATABASE_URL");
  return sandboxPool;
}

/** Everything the application does on its own behalf: query logging, rate-limit reads, auth. */
export function getAppPool(): Pool {
  appPool ??= createPool("APP_RW_DATABASE_URL");
  return appPool;
}

export async function closePools(): Promise<void> {
  const open = [sandboxPool, appPool];
  sandboxPool = null;
  appPool = null;
  await Promise.all(open.map((pool) => pool?.end()));
}

export async function logQuery(entry: QueryLogEntry): Promise<void> {
  await getAppPool().query(
    `INSERT INTO app.query_log
       (user_id, client_ip, query_text, status, duration_ms, row_count, truncated, error_text)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
    [
      entry.userId,
      entry.clientIp,
      entry.queryText,
      entry.status,
      entry.durationMs ?? null,
      entry.rowCount ?? null,
      entry.truncated ?? false,
      entry.errorText ?? null,
    ],
  );
}

export async function executeQuery(
  sql: string,
  ctx: { userId: number; clientIp: string | null },
  opts: { timeoutMs?: number; maxRows?: number } = {},
): Promise<ExecuteResult> {
  const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const maxRows = opts.maxRows ?? DEFAULT_MAX_ROWS;
  const startedAt = Date.now();

  let result: ExecuteResult;
  try {
    const { rows, columns, truncated } = await runInSandbox(sql, timeoutMs, maxRows);
    result = {
      status: "ok",
      rows,
      columns,
      rowCount: rows.length,
      truncated,
      durationMs: Date.now() - startedAt,
    };
  } catch (err) {
    const durationMs = Date.now() - startedAt;
    const code = (err as { code?: string }).code;
    result =
      code === QUERY_CANCELED
        ? { status: "timeout", durationMs }
        : { status: "error", message: messageOf(err), durationMs };
  }

  // Not in a `finally`: a query that cannot be audited must fail the call rather than return
  // results, so this rejection is allowed to propagate (AGENTS.md critical rule).
  await logQuery({
    userId: ctx.userId,
    clientIp: ctx.clientIp,
    queryText: sql,
    status: result.status,
    durationMs: result.durationMs,
    rowCount: result.status === "ok" ? result.rowCount : null,
    truncated: result.status === "ok" ? result.truncated : false,
    errorText: result.status === "error" ? result.message : null,
  });

  return result;
}

async function runInSandbox(
  sql: string,
  timeoutMs: number,
  maxRows: number,
): Promise<{ rows: unknown[][]; columns: string[]; truncated: boolean }> {
  // SET LOCAL takes no bind parameters, so timeoutMs is the one value in this module that ends
  // up interpolated into SQL text. It never comes from the user, but check the shape anyway.
  if (!Number.isInteger(timeoutMs) || timeoutMs <= 0) {
    throw new Error(`timeoutMs must be a positive integer, got ${timeoutMs}`);
  }
  if (!Number.isInteger(maxRows) || maxRows <= 0) {
    throw new Error(`maxRows must be a positive integer, got ${maxRows}`);
  }

  const client = await getSandboxPool().connect();
  let rollbackError: Error | undefined;
  try {
    await client.query("BEGIN READ ONLY");
    await client.query(`SET LOCAL statement_timeout = ${timeoutMs}`);

    // The user's SQL is its own protocol message with its own portal: never concatenated with
    // the wrapper statements, and never rewritten with a LIMIT. The cap is the portal's row
    // count, so an unbounded query is bounded by the server, not by the client draining it.
    const cursor = client.query(new Cursor<unknown[]>(sql, undefined, { rowMode: "array" }));
    const { rows, columns } = await readCursor(cursor, maxRows + 1);
    await cursor.close();

    const truncated = rows.length > maxRows;
    return { rows: truncated ? rows.slice(0, maxRows) : rows, columns, truncated };
  } finally {
    try {
      await client.query("ROLLBACK");
    } catch (err) {
      rollbackError = err as Error;
    }
    // Passing an error to release() destroys the client instead of returning a connection
    // still stuck in an open transaction to the pool.
    client.release(rollbackError);
  }
}

function readCursor(
  cursor: Cursor<unknown[]>,
  rows: number,
): Promise<{ rows: unknown[][]; columns: string[] }> {
  return new Promise((resolve, reject) => {
    cursor.read(rows, (err, read, result) => {
      if (err) reject(err);
      else resolve({ rows: read, columns: result.fields.map((field) => field.name) });
    });
  });
}

function messageOf(err: unknown): string {
  const message = (err as { message?: unknown }).message;
  return typeof message === "string" && message.length > 0 ? message : String(err);
}
