import type { Session } from "next-auth";

import { auth } from "../auth";

type AuthFn = () => Promise<Session | null>;

/**
 * `app.users.id` is an int4, and pg parses int4 as a JS number -- but Auth.js types
 * `session.user.id` as `string`. Both shapes therefore reach this function depending on
 * whether a value came straight off the adapter row or through Auth.js' types, so accept
 * either and reject anything that is not a positive integer.
 */
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

/**
 * The single auth gate for API routes (AGENTS.md: `/api/query` is never anonymous).
 * Returns null for absent, expired, or malformed sessions -- Auth.js already deletes an
 * expired database session and resolves `auth()` to null, so null is the only
 * unauthenticated signal this needs to trust.
 *
 * `authFn` exists solely as a test seam; routes call `requireSession()` with no argument.
 */
export async function requireSession(
  authFn: AuthFn = auth,
): Promise<{ userId: number } | null> {
  const session = await authFn();
  if (!session) return null;

  const user: unknown = session.user;
  if (typeof user !== "object" || user === null) return null;

  const userId = toUserId((user as { id?: unknown }).id);
  return userId === null ? null : { userId };
}
