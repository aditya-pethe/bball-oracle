import { checkRateLimit } from "./rate-limit";
import { executeQuery, getAppPool, logQuery, type QuerySource } from "./execute-query";
import { validateSql } from "./validate-sql";

/**
 * The one execution path for user-authored SQL.
 *
 * Phase 4 gives the chain a second entry point (`/api/internal/query`, for the agent and the
 * eval harness) alongside the session-gated `/api/query`. Composing the chain inline in each
 * route would mean two places where a step could go missing; there is exactly one here, so
 * "rate limit -> validate -> sandbox_ro -> statement timeout -> row cap -> exactly one
 * query_log row" is enforced for every caller by construction (AGENTS.md critical rules).
 *
 * What this module deliberately does NOT do is authenticate. Each route proves who the caller
 * is -- a NextAuth session, or a service token plus an explicit user id -- and hands down a
 * verified `userId` and a `source` it chose itself. There is no code path here that accepts
 * either from a request body.
 */

export const TIMEOUT_MS = 5000;
export const MAX_ROWS = 1000;

export type RunUserSqlResult =
  | { status: "rate_limited"; scope: "user" | "ip"; retryAfterSeconds: number }
  | { status: "validation_rejected"; error: string }
  | {
      status: "ok";
      rows: unknown[][];
      columns: string[];
      rowCount: number;
      truncated: boolean;
      durationMs: number;
    }
  | { status: "timeout"; durationMs: number }
  | { status: "error"; error: string; durationMs: number };

export type RunUserSqlParams = {
  sql: string;
  /** Verified by the caller's own gate. Never read off the request body. */
  userId: number;
  /**
   * null for service callers: their address is a single machine, so per-IP limiting would
   * collapse every agent and eval request into one bucket while telling us nothing about
   * abuse. Per-user limiting still applies and is the meaningful one for an attributed call.
   */
  clientIp: string | null;
  /** Set by the endpoint from the credential that authenticated it. */
  source: QuerySource;
  conversationMessageId?: number | null;
};

export async function runUserSql(params: RunUserSqlParams): Promise<RunUserSqlResult> {
  const { sql, userId, clientIp, source, conversationMessageId = null } = params;

  const rate = await checkRateLimit(getAppPool(), { userId, clientIp });
  if (!rate.allowed) {
    return {
      status: "rate_limited",
      scope: rate.scope,
      retryAfterSeconds: rate.retryAfterSeconds,
    };
  }

  // Defense in depth only -- sandbox_ro's grants are the actual boundary. A rejection here is
  // still an attempt and still gets its audit row.
  const verdict = await validateSql(sql);
  if (!verdict.ok) {
    await logQuery({
      userId,
      clientIp,
      queryText: sql,
      status: "validation_rejected",
      source,
      conversationMessageId,
      errorText: verdict.reason,
    });
    return { status: "validation_rejected", error: verdict.reason };
  }

  // executeQuery owns sandbox_ro + statement timeout + row cap + the log write, and its log
  // write is not in a `finally` -- a query that cannot be audited fails the call.
  const result = await executeQuery(
    sql,
    { userId, clientIp, source, conversationMessageId },
    { timeoutMs: TIMEOUT_MS, maxRows: MAX_ROWS },
  );

  return result.status === "error"
    ? { status: "error", error: result.message, durationMs: result.durationMs }
    : result;
}

/**
 * Shared HTTP mapping so both doors answer identically. The client's failure discrimination
 * (api-types.ts `QueryFailure`) reads the status code, and a 400 that means "validation
 * rejected" is told apart from a 400 that means "SQL error" by the presence of `durationMs`.
 */
export function queryResponse(result: RunUserSqlResult): Response {
  switch (result.status) {
    case "ok":
      return Response.json(result);
    case "rate_limited":
      return Response.json(
        {
          error: `rate limit exceeded (per ${result.scope}) — retry in ${result.retryAfterSeconds}s`,
          retryAfterSeconds: result.retryAfterSeconds,
        },
        { status: 429, headers: { "Retry-After": String(result.retryAfterSeconds) } },
      );
    case "validation_rejected":
      return Response.json({ error: result.error }, { status: 400 });
    case "timeout":
      return Response.json(
        {
          error: `query exceeded the ${TIMEOUT_MS / 1000}s time limit`,
          durationMs: result.durationMs,
        },
        { status: 504 },
      );
    case "error":
      return Response.json({ error: result.error, durationMs: result.durationMs }, { status: 400 });
  }
}

/** `{ sql }` is the shared half of both routes' request bodies. */
export function readSql(body: unknown): string | null {
  const sql = (body as { sql?: unknown })?.sql;
  return typeof sql === "string" && sql.trim() !== "" ? sql : null;
}
