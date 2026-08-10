import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { Client } from "pg";
import type { TestProject } from "vitest/node";

const PG_BIN = "/usr/local/bin";
const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const MIGRATIONS = [
  "0001_create_nba_schema.sql",
  "0002_sandbox_ro_role.sql",
  "0003_app_schema.sql",
  "0004_agent_conversations.sql",
  "0005_agent_budget_index.sql",
];

const TEST_DB = "bball_web_test";
const OWNER_ROLE = "bball_test_owner";
const OWNER_PASSWORD = "bball_test_owner_local_only";
const SANDBOX_PASSWORD = "sandbox_ro_local_test_only";
const APP_RW_PASSWORD = "app_rw_local_test_only";

export const FIXTURE_ROWS = 12;
export const FIXTURE_USER_ID = 1;

function pg(bin: string, args: string[]) {
  return execFileSync(join(PG_BIN, bin), args, { encoding: "utf8" });
}

async function freePort(): Promise<number> {
  for (let port = 54330; port < 54350; port++) {
    const available = await new Promise<boolean>((resolve) => {
      const server = createServer();
      server.once("error", () => resolve(false));
      server.once("listening", () => server.close(() => resolve(true)));
      server.listen(port, "127.0.0.1");
    });
    if (available) return port;
  }
  throw new Error("no free port in 54330-54349 for the test Postgres cluster");
}

function dsn(port: number, user: string, password: string) {
  return `postgresql://${user}:${password}@127.0.0.1:${port}/${TEST_DB}`;
}

async function connect(connectionString: string): Promise<Client> {
  const client = new Client({ connectionString });
  await client.connect();
  return client;
}

async function waitForReady(port: number) {
  for (let attempt = 0; attempt < 40; attempt++) {
    try {
      const client = await connect(`postgresql://postgres@127.0.0.1:${port}/postgres`);
      await client.end();
      return;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
  }
  throw new Error("test Postgres cluster did not become ready");
}

export default async function setup(project: TestProject) {
  const tmp = mkdtempSync(join(tmpdir(), "bball_web_pg_"));
  const dataDir = join(tmp, "data");
  const socketDir = join(tmp, "socket");
  mkdirSync(socketDir);
  const port = await freePort();

  pg("initdb", ["-D", dataDir, "-U", "postgres", "-A", "trust", "--no-sync"]);
  pg("pg_ctl", [
    "-D",
    dataDir,
    "-l",
    join(tmp, "postgres.log"),
    "-w",
    "start",
    "-o",
    `-c listen_addresses=127.0.0.1 -c port=${port} -k ${socketDir} -c fsync=off`,
  ]);

  const stop = () => {
    try {
      pg("pg_ctl", ["-D", dataDir, "-m", "immediate", "stop"]);
    } catch {
      // cluster already gone; the temp dir removal below is what actually matters
    }
    rmSync(tmp, { recursive: true, force: true });
  };

  try {
    await waitForReady(port);

    const root = await connect(`postgresql://postgres@127.0.0.1:${port}/postgres`);
    await root.query(
      `CREATE ROLE ${OWNER_ROLE} LOGIN PASSWORD '${OWNER_PASSWORD}'
         NOSUPERUSER CREATEROLE NOREPLICATION NOBYPASSRLS`,
    );
    await root.query(`CREATE DATABASE ${TEST_DB} OWNER ${OWNER_ROLE}`);
    await root.end();

    // Migration 0002 aborts unless the applying role owns schema `public`, and 0002/0003 must
    // run as a non-superuser CREATEROLE role to mirror Supabase's `postgres`. Hand `public`
    // over exactly like db/tests/conftest.py does.
    const rootInDb = await connect(`postgresql://postgres@127.0.0.1:${port}/${TEST_DB}`);
    await rootInDb.query(`ALTER SCHEMA public OWNER TO ${OWNER_ROLE}`);
    await rootInDb.end();

    const ownerDsn = dsn(port, OWNER_ROLE, OWNER_PASSWORD);
    const owner = await connect(ownerDsn);
    for (const migration of MIGRATIONS) {
      await owner.query(readFileSync(join(REPO_ROOT, "db", "migrations", migration), "utf8"));
    }

    await owner.query(`ALTER ROLE sandbox_ro PASSWORD '${SANDBOX_PASSWORD}'`);
    await owner.query(`ALTER ROLE app_rw PASSWORD '${APP_RW_PASSWORD}'`);

    await owner.query(
      `INSERT INTO app.users (id, name, email) VALUES ($1, 'test', 'test@example.com')`,
      [FIXTURE_USER_ID],
    );
    // Explicit-id inserts don't advance the identity sequence; realign it so tests
    // using `INSERT ... RETURNING id` don't collide with the fixture user.
    await owner.query(
      `SELECT setval(pg_get_serial_sequence('app.users', 'id'), (SELECT max(id) FROM app.users))`,
    );
    await owner.query(
      `INSERT INTO nba.pbp_event
         (season, season_type, game_id, eventnum, eventmsgtype, period,
          pctimestring, homedescription, player1_id, player1_name)
       SELECT 2023, 'regular', 22300001, g, (g % 5) + 1, 1,
              '12:00', 'event ' || g, 200000 + g, 'Player ' || g
       FROM generate_series(1, $1) AS g`,
      [FIXTURE_ROWS],
    );
    await owner.end();

    project.provide("sandboxUrl", dsn(port, "sandbox_ro", SANDBOX_PASSWORD));
    project.provide("appUrl", dsn(port, "app_rw", APP_RW_PASSWORD));
    project.provide("ownerUrl", ownerDsn);
  } catch (err) {
    stop();
    throw err;
  }

  return stop;
}

declare module "vitest" {
  interface ProvidedContext {
    sandboxUrl: string;
    appUrl: string;
    ownerUrl: string;
  }
}
