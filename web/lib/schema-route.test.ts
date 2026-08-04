import { afterAll, beforeAll, beforeEach, describe, expect, inject, it, vi } from "vitest";
import { Pool } from "pg";

vi.mock("./require-session", () => ({ requireSession: vi.fn() }));

import { requireSession } from "./require-session";
import { closePools } from "./execute-query";
import { GET } from "../app/api/schema/route";

const mockSession = vi.mocked(requireSession);

let owner: Pool;
let userId: number;

beforeAll(async () => {
  process.env.SANDBOX_RO_DATABASE_URL = inject("sandboxUrl");
  process.env.APP_RW_DATABASE_URL = inject("appUrl");
  owner = new Pool({ connectionString: inject("ownerUrl"), max: 2 });
  const { rows } = await owner.query(
    "INSERT INTO app.users (name) VALUES ('schema-route-test') RETURNING id",
  );
  userId = rows[0].id;
});

afterAll(async () => {
  await owner.end();
  await closePools();
});

beforeEach(() => {
  mockSession.mockResolvedValue({ userId });
});

describe("auth gate", () => {
  it("returns 401 with no session", async () => {
    mockSession.mockResolvedValue(null);
    const res = await GET();
    expect(res.status).toBe(401);
    expect((await res.json()).error).toBe("authentication required");
  });
});

describe("schema visibility", () => {
  it("lists exactly the tables sandbox_ro has been granted", async () => {
    const res = await GET();
    expect(res.status).toBe(200);
    const body = await res.json();
    const names = body.tables.map((t: { name: string }) => t.name).sort();
    expect(names).toEqual(["pbp_event", "shot_detail"]);

    const pbpEvent = body.tables.find((t: { name: string }) => t.name === "pbp_event");
    const columnNames = pbpEvent.columns.map((c: { name: string }) => c.name);
    expect(columnNames).toContain("game_id");
    expect(columnNames).toContain("eventnum");
    const gameId = pbpEvent.columns.find((c: { name: string }) => c.name === "game_id");
    expect(gameId.type).toBe("bigint");
  });

  it("does not list a table that exists in nba but was never granted to sandbox_ro", async () => {
    await owner.query("CREATE TABLE nba.ungranted_probe (id integer PRIMARY KEY)");

    const res = await GET();
    const body = await res.json();
    const names = body.tables.map((t: { name: string }) => t.name);
    expect(names).not.toContain("ungranted_probe");
  });
});
