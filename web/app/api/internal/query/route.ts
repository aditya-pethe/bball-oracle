import { timingSafeEqual } from "node:crypto";

import { getAppPool, type QuerySource } from "../../../../lib/execute-query";
import { queryResponse, readSql, runUserSql } from "../../../../lib/run-user-sql";

/**
 * The second door into the SQL execution path, for callers with no browser session: the
 * Python agent service and the eval harness.
 *
 * It is service-authenticated AND user-attributed, never anonymous (AGENTS.md: no anonymous
 * execution path, ever). A caller must present a service token *and* name the real user the
 * query is for, so agent traffic inherits that user's rate limit and audit trail. It runs the
 * identical chain as `/api/query` because both call `runUserSql` -- there is no second
 * composition of the safety chain to keep in sync.
 *
 * Provenance comes from WHICH token authenticated, not from the request. A body field named
 * `source` is ignored: a caller that can label its own traffic can launder it, which would
 * make the column worthless for exactly the audit question it exists to answer.
 */

const TOKENS: ReadonlyArray<{ env: string; source: QuerySource }> = [
  { env: "AGENT_SERVICE_TOKEN", source: "agent" },
  { env: "EVAL_SERVICE_TOKEN", source: "eval" },
];

function equals(a: string, b: string): boolean {
  const left = Buffer.from(a, "utf8");
  const right = Buffer.from(b, "utf8");
  // timingSafeEqual throws on a length mismatch, which would leak length by exception; the
  // comparison against `left` keeps the work constant before returning false.
  if (left.length !== right.length) {
    timingSafeEqual(left, left);
    return false;
  }
  return timingSafeEqual(left, right);
}

/**
 * An unset or empty env var authenticates nothing. Without this, a deploy that forgot to set
 * the token would accept `Authorization: Bearer ` from anyone.
 */
function sourceForToken(presented: string | null): QuerySource | null {
  if (!presented) return null;
  let matched: QuerySource | null = null;
  for (const { env, source } of TOKENS) {
    const expected = process.env[env];
    if (expected && equals(expected, presented)) matched = source;
  }
  return matched;
}

function bearerFrom(req: Request): string | null {
  const header = req.headers.get("authorization");
  if (!header) return null;
  const [scheme, ...rest] = header.split(" ");
  if (scheme.toLowerCase() !== "bearer") return null;
  const token = rest.join(" ").trim();
  return token === "" ? null : token;
}

/** Mirrors require-session's tolerance: pg hands back int4 as a number, JSON may carry either. */
function toUserId(raw: unknown): number | null {
  if (typeof raw === "number") {
    return Number.isSafeInteger(raw) && raw > 0 ? raw : null;
  }
  if (typeof raw === "string" && /^[1-9]\d*$/.test(raw)) {
    const parsed = Number(raw);
    return Number.isSafeInteger(parsed) ? parsed : null;
  }
  return null;
}

function toMessageId(raw: unknown): number | null {
  return raw === undefined || raw === null ? null : toUserId(raw);
}

export async function POST(req: Request) {
  const source = sourceForToken(bearerFrom(req));
  if (source === null) {
    return Response.json({ error: "invalid service token" }, { status: 401 });
  }

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return Response.json(
      { error: "request body must be JSON: { \"sql\": \"...\", \"userId\": 1 }" },
      { status: 400 },
    );
  }

  // Caller errors carry a machine-readable `code`. Without one they are
  // indistinguishable from a validator rejection (both are a 400 with no
  // `durationMs`), and the agent would treat "your userId is wrong" as "your
  // SQL is wrong" and burn its whole retry budget redrafting a query that was
  // never the problem. Observed doing exactly that before this existed.
  const sql = readSql(body);
  if (sql === null) {
    return Response.json({ error: "missing sql", code: "missing_sql" }, { status: 400 });
  }

  const userId = toUserId((body as { userId?: unknown })?.userId);
  if (userId === null) {
    return Response.json(
      {
        error: "userId must be a positive integer — service calls are never anonymous",
        code: "invalid_user_id",
      },
      { status: 400 },
    );
  }

  const conversationMessageId = toMessageId(
    (body as { conversationMessageId?: unknown })?.conversationMessageId,
  );

  // Attribution has to be real, not merely well-formed: query_log.user_id is the rate-limit
  // key and the audit trail, and an unknown id would either break the FK mid-chain or (once
  // deleted users exist) silently log to nobody.
  const { rows } = await getAppPool().query("SELECT 1 FROM app.users WHERE id = $1", [userId]);
  if (rows.length === 0) {
    return Response.json(
      { error: `unknown userId ${userId}`, code: "unknown_user" },
      { status: 400 },
    );
  }

  return queryResponse(
    await runUserSql({ sql, userId, clientIp: null, source, conversationMessageId }),
  );
}
