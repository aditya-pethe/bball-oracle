import { requireSession } from "../../../lib/require-session";
import { validateSql } from "../../../lib/validate-sql";
import { executeQuery, getAppPool, logQuery } from "../../../lib/execute-query";
import { checkRateLimit } from "../../../lib/rate-limit";

const TIMEOUT_MS = 5000;
const MAX_ROWS = 1000;

function clientIpFrom(req: Request): string | null {
  const forwarded = req.headers.get("x-forwarded-for");
  if (!forwarded) return null;
  const first = forwarded.split(",")[0].trim();
  return first || null;
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
      { error: "request body must be JSON: { \"sql\": \"...\" }" },
      { status: 400 },
    );
  }
  const sql = (body as { sql?: unknown })?.sql;
  if (typeof sql !== "string" || sql.trim() === "") {
    return Response.json({ error: "missing sql" }, { status: 400 });
  }

  const clientIp = clientIpFrom(req);
  const rate = await checkRateLimit(getAppPool(), { userId: session.userId, clientIp });
  if (!rate.allowed) {
    return Response.json(
      {
        error: `rate limit exceeded (per ${rate.scope}) — retry in ${rate.retryAfterSeconds}s`,
        retryAfterSeconds: rate.retryAfterSeconds,
      },
      { status: 429, headers: { "Retry-After": String(rate.retryAfterSeconds) } },
    );
  }

  const verdict = await validateSql(sql);
  if (!verdict.ok) {
    await logQuery({
      userId: session.userId,
      clientIp,
      queryText: sql,
      status: "validation_rejected",
      errorText: verdict.reason,
    });
    return Response.json({ error: verdict.reason }, { status: 400 });
  }

  const result = await executeQuery(
    sql,
    { userId: session.userId, clientIp },
    { timeoutMs: TIMEOUT_MS, maxRows: MAX_ROWS },
  );

  switch (result.status) {
    case "ok":
      return Response.json(result);
    case "timeout":
      return Response.json(
        {
          error: `query exceeded the ${TIMEOUT_MS / 1000}s time limit`,
          durationMs: result.durationMs,
        },
        { status: 504 },
      );
    case "error":
      return Response.json(
        { error: result.message, durationMs: result.durationMs },
        { status: 400 },
      );
  }
}
