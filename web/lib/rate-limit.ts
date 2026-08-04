export interface QueryablePool {
  query(text: string, values?: unknown[]): Promise<{ rows: Record<string, unknown>[] }>;
}

export type RateLimitResult =
  | { allowed: true }
  | { allowed: false; scope: "user" | "ip"; retryAfterSeconds: number };

export interface RateLimitOptions {
  userLimit?: number;
  ipLimit?: number;
  windowSeconds?: number;
}

const DEFAULTS = { userLimit: 30, ipLimit: 60, windowSeconds: 60 };

async function checkScope(
  pool: QueryablePool,
  column: "user_id" | "client_ip",
  value: number | string,
  limit: number,
  windowSeconds: number,
): Promise<number | null> {
  const cast = column === "client_ip" ? "::inet" : "::integer";
  const { rows } = await pool.query(
    `SELECT count(*)::int AS n FROM app.query_log
     WHERE ${column} = $1${cast} AND started_at > now() - make_interval(secs => $2)`,
    [value, windowSeconds],
  );
  const n = rows[0].n as number;
  if (n < limit) return null;

  const nth = await pool.query(
    `SELECT extract(epoch FROM now() - started_at) AS age FROM app.query_log
     WHERE ${column} = $1${cast} AND started_at > now() - make_interval(secs => $2)
     ORDER BY started_at DESC OFFSET $3 LIMIT 1`,
    [value, windowSeconds, limit - 1],
  );
  const age = nth.rows.length ? Number(nth.rows[0].age) : 0;
  return Math.max(1, Math.ceil(windowSeconds - age));
}

export async function checkRateLimit(
  pool: QueryablePool,
  ctx: { userId: number; clientIp: string | null },
  opts?: RateLimitOptions,
): Promise<RateLimitResult> {
  const { userLimit, ipLimit, windowSeconds } = { ...DEFAULTS, ...opts };

  const userRetry = await checkScope(pool, "user_id", ctx.userId, userLimit, windowSeconds);
  if (userRetry !== null) {
    return { allowed: false, scope: "user", retryAfterSeconds: userRetry };
  }

  if (ctx.clientIp !== null) {
    const ipRetry = await checkScope(pool, "client_ip", ctx.clientIp, ipLimit, windowSeconds);
    if (ipRetry !== null) {
      return { allowed: false, scope: "ip", retryAfterSeconds: ipRetry };
    }
  }

  return { allowed: true };
}
