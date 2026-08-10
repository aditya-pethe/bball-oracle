/**
 * The agent's spend controls.
 *
 * The load-bearing case is the one `checkRateLimit` cannot cover: a turn that makes a model
 * call and executes no SQL (`clarify` / `decline`) writes no `app.query_log` row at all, so a
 * query_log-based count leaves it unlimited. These tests count assistant turns instead and
 * assert that a user whose turns never touched the executor still hits the cap.
 */
import { afterAll, afterEach, beforeAll, describe, expect, inject, it } from "vitest";
import { Pool } from "pg";

import {
  DEFAULT_DAILY_MESSAGE_LIMIT,
  agentEnabled,
  checkAgentBudget,
  dailyMessageLimit,
} from "./agent-budget";
import { closePools } from "./execute-query";

let owner: Pool;
let userA: number;
let userB: number;

async function newConversation(userId: number): Promise<number> {
  const { rows } = await owner.query(
    "INSERT INTO app.conversation (user_id, title) VALUES ($1, 'budget test') RETURNING id",
    [userId],
  );
  return Number(rows[0].id);
}

async function addMessages(
  conversationId: number,
  role: "user" | "assistant",
  count: number,
  createdAt?: string,
) {
  for (let i = 0; i < count; i++) {
    await owner.query(
      `INSERT INTO app.conversation_message (conversation_id, role, content, created_at)
       VALUES ($1, $2, '{}'::jsonb, COALESCE($3::timestamptz, now()))`,
      [conversationId, role, createdAt ?? null],
    );
  }
}

beforeAll(async () => {
  process.env.APP_RW_DATABASE_URL = inject("appUrl");
  process.env.SANDBOX_RO_DATABASE_URL = inject("sandboxUrl");
  owner = new Pool({ connectionString: inject("ownerUrl"), max: 2 });
  const { rows } = await owner.query(
    `INSERT INTO app.users (name) VALUES ('budget-test-a'), ('budget-test-b') RETURNING id`,
  );
  userA = rows[0].id;
  userB = rows[1].id;
});

afterAll(async () => {
  await owner.end();
  await closePools();
});

afterEach(() => {
  delete process.env.AGENT_ENABLED;
  delete process.env.AGENT_DAILY_MESSAGE_LIMIT;
});

describe("agentEnabled", () => {
  it("fails closed when AGENT_ENABLED is unset", () => {
    expect(agentEnabled({})).toBe(false);
  });

  it("is off for empty, false, 0, off and anything unrecognised", () => {
    for (const value of ["", "  ", "false", "0", "off", "no", "maybe"]) {
      expect(agentEnabled({ AGENT_ENABLED: value })).toBe(false);
    }
  });

  it("is on for true/1/yes/on, case- and whitespace-insensitively", () => {
    for (const value of ["true", "TRUE", " True ", "1", "yes", "on"]) {
      expect(agentEnabled({ AGENT_ENABLED: value })).toBe(true);
    }
  });
});

describe("dailyMessageLimit", () => {
  it("defaults to 25 when unset", () => {
    expect(dailyMessageLimit({})).toBe(DEFAULT_DAILY_MESSAGE_LIMIT);
    expect(DEFAULT_DAILY_MESSAGE_LIMIT).toBe(25);
  });

  it("reads a positive integer override", () => {
    expect(dailyMessageLimit({ AGENT_DAILY_MESSAGE_LIMIT: "3" })).toBe(3);
  });

  it("honours 0 as a per-user off switch", () => {
    expect(dailyMessageLimit({ AGENT_DAILY_MESSAGE_LIMIT: "0" })).toBe(0);
  });

  it("falls back to the default on a malformed value rather than to unlimited", () => {
    for (const value of ["-5", "abc", "1.5", "1e3", ""]) {
      expect(dailyMessageLimit({ AGENT_DAILY_MESSAGE_LIMIT: value })).toBe(
        DEFAULT_DAILY_MESSAGE_LIMIT,
      );
    }
  });
});

describe("checkAgentBudget", () => {
  it("allows a user with no history", async () => {
    const result = await checkAgentBudget(owner, userB, { limit: 2 });
    expect(result).toMatchObject({ allowed: true, used: 0, limit: 2 });
    expect(result.retryAfterSeconds).toBeGreaterThan(0);
  });

  it("counts assistant turns that executed no SQL at all", async () => {
    const conversation = await newConversation(userA);
    await addMessages(conversation, "assistant", 2);

    // Nothing was written to query_log by these turns -- which is exactly why the budget
    // cannot be counted from it.
    const { rows } = await owner.query(
      "SELECT count(*)::int AS n FROM app.query_log WHERE user_id = $1",
      [userA],
    );
    expect(rows[0].n).toBe(0);

    expect(await checkAgentBudget(owner, userA, { limit: 2 })).toMatchObject({
      allowed: false,
      used: 2,
      limit: 2,
    });
  });

  it("ignores user turns — the cap counts answers, not keystrokes", async () => {
    const conversation = await newConversation(userB);
    await addMessages(conversation, "user", 5);
    expect(await checkAgentBudget(owner, userB, { limit: 2 })).toMatchObject({
      allowed: true,
      used: 0,
    });
  });

  it("is per user: one user's spend never counts against another's", async () => {
    expect(await checkAgentBudget(owner, userA, { limit: 2 })).toMatchObject({ allowed: false });
    expect(await checkAgentBudget(owner, userB, { limit: 2 })).toMatchObject({
      allowed: true,
      used: 0,
    });
  });

  it("ignores turns from before the current UTC day", async () => {
    const { rows } = await owner.query(
      "INSERT INTO app.users (name) VALUES ('budget-test-yesterday') RETURNING id",
    );
    const user = rows[0].id;
    const conversation = await newConversation(user);
    await addMessages(conversation, "assistant", 40, "2024-01-01T12:00:00Z");

    expect(await checkAgentBudget(owner, user, { limit: 1 })).toMatchObject({
      allowed: true,
      used: 0,
    });
  });

  it("reports a retryAfterSeconds no larger than a day", async () => {
    const { retryAfterSeconds } = await checkAgentBudget(owner, userB, { limit: 1 });
    expect(retryAfterSeconds).toBeGreaterThan(0);
    expect(retryAfterSeconds).toBeLessThanOrEqual(86_400);
  });

  it("reads the limit from AGENT_DAILY_MESSAGE_LIMIT when none is passed", async () => {
    process.env.AGENT_DAILY_MESSAGE_LIMIT = "1";
    expect(await checkAgentBudget(owner, userA)).toMatchObject({ allowed: false, limit: 1 });
    process.env.AGENT_DAILY_MESSAGE_LIMIT = "100";
    expect(await checkAgentBudget(owner, userA)).toMatchObject({ allowed: true, limit: 100 });
  });
});
