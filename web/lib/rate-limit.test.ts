import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { Pool } from "pg";
import { checkRateLimit } from "./rate-limit";

const dsn = process.env.APP_RW_DATABASE_URL;
if (!dsn) {
  throw new Error("rate-limit tests require APP_RW_DATABASE_URL (a test-cluster app_rw DSN)");
}

let pool: Pool;

beforeAll(() => {
  pool = new Pool({ connectionString: dsn, max: 2 });
});

afterAll(async () => {
  await pool.end();
});

// app_rw has no DELETE on query_log (append-only by design), so tests isolate by
// fresh users/IPs instead of cleanup.
async function newUser(): Promise<number> {
  const { rows } = await pool.query(
    "INSERT INTO app.users (name) VALUES ('rate-limit-test') RETURNING id",
  );
  return rows[0].id;
}

function newIp(): string {
  const octet = () => Math.floor(Math.random() * 254) + 1;
  return `10.${octet()}.${octet()}.${octet()}`;
}

async function logRow(userId: number, clientIp: string | null, ageSeconds = 0, status = "ok") {
  await pool.query(
    `INSERT INTO app.query_log (user_id, client_ip, query_text, status, started_at)
     VALUES ($1, $2, 'SELECT 1', $3, now() - make_interval(secs => $4))`,
    [userId, clientIp, status, ageSeconds],
  );
}

const opts = { userLimit: 3, ipLimit: 5, windowSeconds: 60 };

describe("per-user limit", () => {
  it("allows under the limit", async () => {
    const userId = await newUser();
    await logRow(userId, newIp());
    await logRow(userId, newIp());
    const result = await checkRateLimit(pool, { userId, clientIp: newIp() }, opts);
    expect(result).toEqual({ allowed: true });
  });

  it("blocks at the limit with a sane retryAfter", async () => {
    const userId = await newUser();
    for (let i = 0; i < 3; i++) await logRow(userId, newIp());
    const result = await checkRateLimit(pool, { userId, clientIp: newIp() }, opts);
    expect(result.allowed).toBe(false);
    if (!result.allowed) {
      expect(result.scope).toBe("user");
      expect(result.retryAfterSeconds).toBeGreaterThan(0);
      expect(result.retryAfterSeconds).toBeLessThanOrEqual(60);
    }
  });

  it("ignores rows outside the window", async () => {
    const userId = await newUser();
    for (let i = 0; i < 3; i++) await logRow(userId, newIp(), 120);
    const result = await checkRateLimit(pool, { userId, clientIp: newIp() }, opts);
    expect(result).toEqual({ allowed: true });
  });

  it("counts rejected attempts toward the window", async () => {
    const userId = await newUser();
    for (let i = 0; i < 3; i++) await logRow(userId, newIp(), 0, "validation_rejected");
    const result = await checkRateLimit(pool, { userId, clientIp: newIp() }, opts);
    expect(result.allowed).toBe(false);
  });

  it("is not affected by other users' activity", async () => {
    const busy = await newUser();
    const quiet = await newUser();
    for (let i = 0; i < 3; i++) await logRow(busy, newIp());
    const result = await checkRateLimit(pool, { userId: quiet, clientIp: newIp() }, opts);
    expect(result).toEqual({ allowed: true });
  });
});

describe("per-IP limit", () => {
  it("blocks a shared IP across different users", async () => {
    const ip = newIp();
    for (let i = 0; i < 5; i++) await logRow(await newUser(), ip);
    const result = await checkRateLimit(pool, { userId: await newUser(), clientIp: ip }, opts);
    expect(result.allowed).toBe(false);
    if (!result.allowed) expect(result.scope).toBe("ip");
  });

  it("skips the IP check when clientIp is null", async () => {
    const ip = newIp();
    for (let i = 0; i < 5; i++) await logRow(await newUser(), ip);
    const result = await checkRateLimit(pool, { userId: await newUser(), clientIp: null }, opts);
    expect(result).toEqual({ allowed: true });
  });
});
