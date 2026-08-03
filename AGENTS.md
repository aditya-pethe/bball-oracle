# bball-oracle — Agent Instructions

## Critical rules (non-negotiable)
- ALWAYS treat `project_plan.md` as the PRD. If a task requires deviating from it, update `project_plan.md` in the same change — never drift silently.
- ALWAYS gate `/api/query` behind a valid NextAuth session. NEVER add an anonymous execution path, even for local testing convenience.
- ALWAYS execute user SQL through the restricted `sandbox_ro` Postgres role (SELECT-only on `nba.pbp_event`, `nba.shot_detail`; no access to the `app` schema). Application-level SQL validation is defense in depth ONLY — never treat it as the security boundary.
- ALWAYS enforce statement timeout, row cap, and query-log write together on every query execution path. NEVER ship a code path that skips one of the three.
- NEVER add monetization (ads, paid tiers, reselling data/API access) without first revisiting `project_plan.md` §9 (data licensing).

## Project context
Hosted NBA analytics app: authenticated users write arbitrary read-only SQL against a real NBA play-by-play dataset and see results in a UI. MVP goal: sign-in → SQL sandbox → real data → results, working end-to-end, safely and cheaply. `project_plan.md` is the full product spec (scope, architecture, data model); this file is operational rules for agents working in this repo, not a restatement of it.

## Status
Phase 0 (data pipeline) complete and merged to `main`: schema (`db/migrations/0001`) and `sandbox_ro` role (`db/migrations/0002`) are applied to the live Supabase project, and the 2023-24 regular season is loaded for real (`nba.pbp_event`/`nba.shot_detail`, 786K rows total, 216MB) — see Resolved during Phase 0 for why this is a deliberately narrower window than originally proposed.

Phase 1 (query API) implemented on `feat/query-api`: Next.js 16 app in `web/`; migration `0003` (schema `app` + `app_rw` role) applied to the live project; both roles have passwords provisioned (in `.env`, never committed); `/api/query` composes session gate → Postgres rate limit → libpg-query validator → `sandbox_ro` executor, with every post-auth attempt writing exactly one `app.query_log` row. All suites green (web 107, grants 90, ETL 27). Still open before merging to `main`: GitHub OAuth app creds (user), live E2E sign-in → query, Vercel project wiring.

## Permission mode
Claude Code defaults to `auto` mode in this project (`.claude/settings.json`, `permissions.defaultMode: "auto"`) — this applies to the main session and to subagents spawned here via the Agent/Task tool. If you're a different AGENTS.md-compatible tool (Cursor, Aider, Codex, etc.), this setting doesn't apply to you — configure your own auto/approval mode separately; there's no single file that controls it across tools.

## Workflow

### Branching
Trunk-based, no long-lived `dev` branch. `main` stays always-deployable; work happens in short-lived feature branches (`feat/etl-pipeline`, `feat/nextauth-setup`); Vercel gives a preview deploy per branch/PR; merge to `main` once verified. Reintroduce a `dev` branch only if this stops being a solo effort and a stabilization buffer before release becomes necessary — premature otherwise.

### Agent orchestration
- The primary session is the orchestrator: stays in the main worktree, holds the plan, reviews diffs, merges to `main`, keeps this file and `project_plan.md` current.
- Spawn a subagent in an isolated worktree only when a workstream is (a) large/self-contained enough to brief once and check on later, and (b) actually independent of whatever's being edited in the main tree — e.g. the data pipeline and the auth scaffolding running at the same time once both are active. Small or local edits happen directly in the main session; spinning up a worktree for a 10-line change is pure overhead.
- Use non-isolated research subagents (search/investigation only, no writes) to keep exploration out of the primary session's context — not for tasks that produce code.

### Model selection by task
Match model tier to task complexity, not to habit or tool. Same three tiers whether the session is Claude Code or Codex:

| Tier | Claude Code | Codex (GPT-5.6) | Use for |
|---|---|---|---|
| Cheap | Haiku | Luna | Mechanical, low-ambiguity work: codebase search/lookups, boilerplate/scaffolding, formatting, well-specified bug fixes with a clear repro. |
| Default | Sonnet | Terra | Most implementation work: typical feature code, refactors, tests, standard debugging. Use this when unsure, then escalate if the task turns out to need judgment. |
| High | Opus | Sol | Anything touching a critical rule above (session gating, `sandbox_ro` execution path, timeout/row-cap/query-log enforcement), schema/architecture decisions, ambiguous requirements, or planning/reviewing a large workstream before handing it to a worktree subagent. |

Verify exact Codex model IDs against the Codex CLI config before relying on this table — GPT-5.6 tier names may not be the literal `--model` flag values.

## Development principles

### Test-driven development
Write tests before implementation whenever the task allows it: enumerate edge cases and failure modes (bad input, boundary conditions, security boundaries) as test cases first, then implement to satisfy them. This is a strong fit for parsing/cleaning logic and for anything implementing a critical rule above — state the required behavior as a test (e.g. `sandbox_ro` rejects mutations and cross-schema access) before writing the code that's supposed to guarantee it. It's a weaker fit for pure scaffolding/config where there's nothing yet to assert. When a phase plan (`.agents/pN_*.md`) lists an implementation step, its tests are part of that step, not a follow-up task.

### Phased planning + task scoping
Every phase gets a detailed plan in `.agents/pN_*.md` before implementation starts, broken into ordered tasks with explicit dependencies. Whether a task is worth a worktree subagent follows the same bar as Workflow ↑ (self-contained + actually independent) — a plan having many small steps doesn't by itself mean those steps parallelize. Check the dependency chain, not the step count, before spawning; a plan may turn out to be almost entirely sequential with one real fork point, which is still worth writing down explicitly so it isn't re-derived later.

## MCP servers
Project-scoped in `.mcp.json` (committed — shared with anyone who clones the repo, no secrets in the file):
- **supabase** — HTTP transport, `project_ref=zblvjxuaqhjlnuprgemx`, scoped to docs/account/database/debugging/development/branching features. Auth is per-user OAuth via `claude /mcp` — nobody needs to hold or paste a static token.
- **playwright** — stdio, `npx @playwright/mcp@latest`. For E2E browser automation once the app exists (sign-in → SQL query → results flow).

A server added mid-session isn't available in that session — `claude mcp list` showing "Connected" only confirms the server itself is reachable; the running session still needs a restart to load its tools.

## Stack
- App: Next.js (App Router) + TypeScript, deployed on Vercel.
- Auth: NextAuth (Auth.js).
- DB: Supabase Postgres, direct connection from API routes (not PostgREST — arbitrary SQL needs a real connection).
- SQL editor: CodeMirror 6.
- Data pipeline: Python (pandas + a Postgres client), run on-demand/offline — not a deployed service.

## Data model
- `nba.pbp_event` — play-by-play events (`nba_data`'s `nbastats` source).
- `nba.shot_detail` — shot attempts with location/zone (`nba_data`'s `shotdetail` source).
- Both are event-grain — no box-score/season/dimension tables exist upstream.
- Season coverage starts recent-only (proposed default 2020-21 through 2024-25, unconfirmed) and backfills later. Build the ETL pipeline season-parameterized so backfill is a config change + rerun, not a rewrite.

## Code style
- Python for the data pipeline, TypeScript/React for the app — no other languages without a stated reason.
- No comments unless the WHY is non-obvious (hidden constraint, workaround, surprising behavior).
- No speculative abstractions — build for current MVP scope per `project_plan.md`, not hypothetical future requirements.

## Commands
Web app (Phase 1+), from `web/`:
```
npm install
npm test         # vitest; spins up/tears down a disposable local Postgres itself
                 # (needs initdb/pg_ctl at /usr/local/bin, ports 54330-54349)
npm run dev      # needs .env values: APP_RW_DATABASE_URL, SANDBOX_RO_DATABASE_URL,
                 # AUTH_SECRET, AUTH_GITHUB_ID, AUTH_GITHUB_SECRET (web/.env.local)
npm run build && npx tsc --noEmit && npm run lint
```

Data pipeline (Phase 0), from repo root:
```
python3 -m venv .venv
.venv/bin/pip install -r pipeline/requirements.txt

# Run the ETL (needs a real Postgres DSN with write access — sandbox_ro is SELECT-only
# and cannot be used here):
NBA_PIPELINE_DATABASE_URL=postgresql://... .venv/bin/python -m pipeline.cli \
  [--config pipeline/seasons.yaml] [--cache-dir .cache/nba_data]

# ETL test suite (spins up/tears down a disposable local Postgres itself, no setup needed):
.venv/bin/python -m pytest tests/

# sandbox_ro grant test suite (needs its own admin DSN against a local, disposable
# Postgres — refuses to run against anything matching supabase/pooler/rds):
.venv/bin/pip install -r db/tests/requirements.txt
BBALL_TEST_ADMIN_DSN=postgresql://postgres@127.0.0.1:5432/postgres .venv/bin/pytest db/tests
```

## File organization
- `db/migrations/NNNN_name.sql` — versioned SQL migrations, applied by hand via Supabase MCP (`apply_migration`), reviewed before applying. `0001` creates schema `nba` (`pbp_event`, `shot_detail`); `0002` creates the `sandbox_ro` role and its grants; `0003` creates schema `app` (NextAuth adapter tables, `query_log`) and the `app_rw` role (table-scoped DML on `app` only, `query_log` append-only).
- `web/` — the Next.js app. `web/lib/` holds the safety chain as separate tested modules: `validate-sql.ts` (pure libpg-query AST validator — defense in depth, NOT the boundary), `execute-query.ts` (`sandbox_ro` execution wrapper + query logging), `rate-limit.ts` (sliding window over `app.query_log`), `require-session.ts` (the only auth gate); `web/app/api/query/route.ts` composes them. `web/lib/test-cluster.ts` is the vitest globalSetup that builds the disposable Postgres (applies all of `db/migrations/`).
- `db/tests/` — pytest/psycopg2 suite asserting on `sandbox_ro`'s actual grants (not application behavior) against a disposable local Postgres. Lives next to `db/migrations/` rather than the repo-root `tests/` since it's testing the data layer, not the pipeline.
- `pipeline/` — the Python ETL package (config, download, transform, load, CLI). `pipeline/seasons.yaml` is the season-coverage config; editing it and rerunning is the entire backfill mechanism, no code changes.
- `tests/` — pytest suite for the ETL pipeline (parse/clean edge cases, load/idempotency), fixtures built from real downloaded sample data, not invented.
- `.agents/` — gitignored scratch space; each phase's investigation/design decision logs live here (e.g. `p0_source_investigation.md`, `p0_schema_design.md`, `p0_sandbox_ro_design.md`, `p0_etl_design.md`) as reasoning context for later tasks, not as product docs.

## Verification
A task touching the web app isn't done until `npm test` (from `web/`), `npx tsc --noEmit`, and `npm run build` all pass. Anything touching the safety chain (validator, executor, rate limit, session gate, or the route composing them) must keep the route integration tests and the executor's three-together assertions (timeout / row cap / log) green — those tests are the enforcement of the critical rules above, not a formality.

A task touching the data layer isn't done until:
1. `.venv/bin/python -m pytest tests/` passes (ETL parse/clean/load/idempotency).
2. `BBALL_TEST_ADMIN_DSN=... .venv/bin/pytest db/tests` passes (sandbox_ro grants) — mandatory for any change to `db/migrations/0002_sandbox_ro_role.sql`, since this suite is the definition of "correctly restricted," not a formality.
3. For a schema or grants change: a migration file exists, was reviewed, and was applied to the live Supabase project via `apply_migration` (never applied ad hoc without a corresponding file in `db/migrations/`).

## Open decisions
- Phase 1 decisions (OAuth: GitHub only; rate limiting: Postgres-based over `app.query_log`; sessions: database strategy for server-side revocation — reasoning in `web/auth.ts`) all resolved.
- Blocking Phase 1 close-out, not further work: GitHub OAuth app credentials (user action), Vercel project wiring, and confirming the Supavisor pooler username format for custom roles (`sandbox_ro.<project_ref>` / `app_rw.<project_ref>`) when the Vercel DSNs are configured — local dev uses the direct connection.

## Resolved during Phase 0
- **Season window:** 2023-24 regular season only, loaded into the live Supabase project (567,662 `pbp_event` rows, 218,701 `shot_detail` rows). Measured at 216MB for one regular season (no playoffs) — the originally proposed 5-season regular+playoffs window would exceed 1GB. Decision: validate the MVP on one season on free tier first; expand via `pipeline/seasons.yaml` + Supabase Pro upgrade once validated, not before. See `project_plan.md` §3/§10.
- **Supabase tier/budget:** confirmed free tier, by design scoped to the one-season window above.
- Supabase API "exposed schemas" setting — not independently verified that `nba` is excluded from PostgREST exposure. `get_advisors` flagged RLS-disabled on both new tables (expected, since the app reaches them only via a direct `sandbox_ro` Postgres connection, never PostgREST) but that's a design assumption, not a verified setting. Worth a manual dashboard check (Settings → API → Exposed schemas) before Phase 2.
- Supavisor pooler username format for `sandbox_ro` (likely `sandbox_ro.<project_ref>`, per `.agents/p0_sandbox_ro_design.md` §6) and the right `CONNECTION LIMIT` value (currently `10`, a guess) — resolve when wiring Phase 1's connection string.

## Common pitfalls
None logged yet. Add an entry here only after the same mistake happens twice (two-strikes rule) — don't pre-populate with hypothetical failure modes.
