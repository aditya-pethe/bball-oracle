import type { QueryablePool } from "./rate-limit";

/**
 * The agent's spend controls: a kill switch and a per-user daily message cap.
 *
 * These are not the same thing as `rate-limit.ts`, and one cannot stand in for the other.
 * `checkRateLimit` counts rows in `app.query_log`, so it only sees turns that executed SQL --
 * a `clarify` or `decline` outcome makes a model call and logs nothing, which left those
 * requests completely uncapped. The budget therefore counts assistant turns in
 * `app.conversation_message`, which is one row per turn regardless of outcome.
 *
 * Both live here rather than inside the route so they can be tested against a real database
 * without a live agent service, and so the route reads as a sequence of gates.
 */

export const DEFAULT_DAILY_MESSAGE_LIMIT = 25;

const TRUTHY = new Set(["1", "true", "yes", "on"]);

/**
 * Fails CLOSED: an unset `AGENT_ENABLED` disables the agent, exactly as an unset
 * `AGENT_SERVICE_TOKEN` authenticates nobody (web/app/api/internal/query/route.ts). The agent
 * is the one part of this app that spends real money per request against the owner's API key,
 * so a deploy that forgets the variable must be inert rather than open.
 */
export function agentEnabled(env: Record<string, string | undefined> = process.env): boolean {
  return TRUTHY.has((env.AGENT_ENABLED ?? "").trim().toLowerCase());
}

/**
 * `AGENT_DAILY_MESSAGE_LIMIT`, or 25 (.agents/p4_agent.md "Budget shape"). A malformed value
 * falls back to the default rather than to "unlimited": a typo in an env var must not be a
 * way to remove the cap. `0` is honoured, and is a second, per-user-visible kill switch.
 */
export function dailyMessageLimit(env: Record<string, string | undefined> = process.env): number {
  const raw = (env.AGENT_DAILY_MESSAGE_LIMIT ?? "").trim();
  if (!/^\d+$/.test(raw)) return DEFAULT_DAILY_MESSAGE_LIMIT;
  const parsed = Number(raw);
  return Number.isSafeInteger(parsed) ? parsed : DEFAULT_DAILY_MESSAGE_LIMIT;
}

export type AgentBudgetResult = {
  allowed: boolean;
  used: number;
  limit: number;
  /** Seconds until the window resets (next UTC midnight) — the 429's Retry-After. */
  retryAfterSeconds: number;
};

/**
 * Counts this user's assistant turns since UTC midnight.
 *
 * The day boundary is UTC and computed by Postgres, not by the Node process: the app runs on
 * Vercel where the function's local timezone is not something to reason about, and two
 * requests must never disagree about which day they are in.
 *
 * Backed by db/migrations/0005's partial index on (conversation_id, created_at).
 */
export async function checkAgentBudget(
  pool: QueryablePool,
  userId: number,
  opts: { limit?: number } = {},
): Promise<AgentBudgetResult> {
  const limit = opts.limit ?? dailyMessageLimit();

  const { rows } = await pool.query(
    `SELECT
       (SELECT count(*)
          FROM app.conversation_message m
          JOIN app.conversation c ON c.id = m.conversation_id
         WHERE c.user_id = $1
           AND m.role = 'assistant'
           AND m.created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
       )::int AS used,
       ceil(extract(epoch FROM
         ((date_trunc('day', now() AT TIME ZONE 'UTC') + interval '1 day') AT TIME ZONE 'UTC') - now()
       ))::int AS resets_in`,
    [userId],
  );

  const used = Number(rows[0].used);
  const retryAfterSeconds = Math.max(1, Number(rows[0].resets_in));

  return { allowed: used < limit, used, limit, retryAfterSeconds };
}
