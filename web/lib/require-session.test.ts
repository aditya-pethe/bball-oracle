import { describe, expect, it, vi } from "vitest";
import type { Session } from "next-auth";

import { requireSession } from "./require-session";

// Stubbed so importing require-session never pulls in auth.ts, which builds a pg Pool
// and reads AUTH_* env at module load. These tests inject their own auth fn instead.
vi.mock("../auth", () => ({ auth: vi.fn() }));

const authReturning = (session: unknown) => () =>
  Promise.resolve(session as Session | null);

const sessionWithUser = (user: unknown) =>
  ({
    expires: new Date(Date.now() + 60_000).toISOString(),
    user,
  }) as unknown as Session;

describe("requireSession", () => {
  it("returns null when there is no session", async () => {
    expect(await requireSession(authReturning(null))).toBeNull();
  });

  it("returns null for an expired database session", async () => {
    // Auth.js deletes an expired database session and resolves auth() to null, so an
    // expired session is indistinguishable from no session here. Trust null.
    expect(await requireSession(authReturning(null))).toBeNull();
  });

  it("returns null when the session carries no user", async () => {
    const session = { expires: new Date().toISOString() };
    expect(await requireSession(authReturning(session))).toBeNull();
  });

  it("returns null when the session user has no id", async () => {
    const session = sessionWithUser({ email: "a@b.com" });
    expect(await requireSession(authReturning(session))).toBeNull();
  });

  it("returns null when the user id is null or empty", async () => {
    for (const id of [null, undefined, "", "   "]) {
      const session = sessionWithUser({ id });
      expect(await requireSession(authReturning(session))).toBeNull();
    }
  });

  it("returns the numeric userId when the adapter supplies a number", async () => {
    // The real runtime shape: pg parses app.users.id (int4) as a JS number even though
    // Auth.js types it as string.
    const session = sessionWithUser({ id: 42 });
    expect(await requireSession(authReturning(session))).toEqual({ userId: 42 });
  });

  it("returns the numeric userId when the id arrives as a string", async () => {
    const session = sessionWithUser({ id: "42" });
    const result = await requireSession(authReturning(session));
    expect(result).toEqual({ userId: 42 });
    expect(typeof result?.userId).toBe("number");
  });

  it("returns null for ids that are not positive integers", async () => {
    for (const id of ["abc", "4.2", "-1", "0", "42abc", 4.2, -1, 0, NaN]) {
      const session = sessionWithUser({ id });
      expect(await requireSession(authReturning(session))).toBeNull();
    }
  });

  it("does not treat a non-object user as authenticated", async () => {
    for (const user of ["42", 42, true]) {
      const session = sessionWithUser(user);
      expect(await requireSession(authReturning(session))).toBeNull();
    }
  });
});
