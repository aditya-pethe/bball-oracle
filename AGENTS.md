# bball-oracle — Agent Instructions

## Critical rules (non-negotiable)
- ALWAYS treat `project_plan.md` as the PRD. If a task requires deviating from it, update `project_plan.md` in the same change — never drift silently.
- ALWAYS gate `/api/query` behind a valid NextAuth session. NEVER add an anonymous execution path, even for local testing convenience. Phase 4 added a **second door**, `/api/internal/query`, for the agent service and eval harness: it is service-token-gated AND requires an explicit `userId` verified against `app.users`, so it is authenticated and user-attributed, never anonymous. The Python service's own `POST /agent` carries the same obligation and is token-gated too — an unauthenticated agent endpoint is an anonymous execution path wearing a costume, since the service holds a token that opens the internal door.
- ALWAYS set `query_log.source` from the credential that authenticated the request, NEVER from the request body. A caller that can label its own provenance can launder it, which destroys the one question the column exists to answer.
- ALWAYS execute user SQL through the restricted `sandbox_ro` Postgres role (SELECT-only on `nba.pbp_event`, `nba.shot_detail`; no access to the `app` schema). Application-level SQL validation is defense in depth ONLY — never treat it as the security boundary.
- ALWAYS enforce statement timeout, row cap, and query-log write together on every query execution path. NEVER ship a code path that skips one of the three.
- NEVER add monetization (ads, paid tiers, reselling data/API access) without first revisiting `project_plan.md` §9 (data licensing).

## Project context
Hosted NBA analytics app: authenticated users write arbitrary read-only SQL against a real NBA play-by-play dataset and see results in a UI. MVP goal: sign-in → SQL sandbox → real data → results, working end-to-end, safely and cheaply. `project_plan.md` is the full product spec (scope, architecture, data model); this file is operational rules for agents working in this repo, not a restatement of it.

## Status
Phase 0 (data pipeline) complete and merged to `main`: schema (`db/migrations/0001`) and `sandbox_ro` role (`db/migrations/0002`) are applied to the live Supabase project, and the 2023-24 regular season is loaded for real (`nba.pbp_event`/`nba.shot_detail`, 786K rows total, 216MB) — see Resolved during Phase 0 for why this is a deliberately narrower window than originally proposed.

Phase 1 (query API) complete and merged to `main`: Next.js 16 app in `web/`; migration `0003` (schema `app` + `app_rw` role) applied to the live project; both roles have passwords provisioned (in `.env`, never committed); `/api/query` composes session gate → Postgres rate limit → libpg-query validator → `sandbox_ro` executor, with every post-auth attempt writing exactly one `app.query_log` row.

Phase 2 (sandbox UI) complete and merged to `main`: Tailwind v4 with a semantic `@theme` token layer (restyling = token/class edits only); styled `/signin`, session-aware `/` stub, server-guarded `/sandbox` (CSR client component, no middleware); session-gated `GET /api/schema` (via `sandbox_ro`, so visibility provably equals grants) and `GET /api/history` (own `app.query_log` rows, LIMIT 50); CodeMirror 6 editor (`@uiw/react-codemirror`, PostgreSQL dialect, schema autocomplete, Cmd/Ctrl+Enter), headless TanStack results table behind a swappable result-view seam, distinct UI states for validation/SQL-error/timeout/rate-limit, 8 example queries verified against the live season, and localStorage editor persistence. All suites green (web 127, grants 90, ETL 27); full flow (sign-out → guard redirect → GitHub sign-in → schema/autocomplete → run → rejection → history) E2E-verified locally and on `https://bball-oracle.vercel.app`. Second-user history isolation is asserted at the API-test level (two-user vitest case), not browser-E2E'd (single GitHub account).

Phase 3 (landing page, attribution, design pass) complete and merged to `main`: real landing page on `/` (pitch, live coverage numbers, verified example teaser with hardcoded real results, session-aware CTA), layout-level attribution/disclaimer footer on every page (satisfies `project_plan.md` §9's attribution requirement), token-value refinements + global focus-visible ring, real page metadata. MVP is feature-complete per `project_plan.md` §12; remaining known follow-up: wire the Vercel git integration via dashboard (deploys are still CLI `npx vercel deploy --prod`).

Phase 4 (agent foundation) built on `feat/p4-agent-foundation`, **not yet merged**: migration `0004` applied to the live project (`app.conversation`, `app.conversation_message`, `query_log.source` + `conversation_message_id`); the Phase 1 safety chain extracted into `web/lib/run-user-sql.ts` and exposed through a second, service-token-gated door at `/api/internal/query`; a Python FastAPI + LangGraph agent service in `agent/`; an eval harness in `evals/`; and the Agent tab in `web/components/`. The full loop is verified end-to-end locally (browser-less: `POST /agent` → graph → `/api/internal/query` → `sandbox_ro` → live data, landing in `query_log` with `source='agent'`). **Not deployed** — the Python service has no Fly app yet, and the Vercel env vars (`AGENT_SERVICE_URL`, `AGENT_SERVICE_TOKEN`) are unset, so the Agent tab is non-functional in production. Zero-shot baseline of record: 64.7% execution accuracy, 100% abstention precision (`evals/runs/`); the LangGraph agent itself is **unmeasured** — see `.agents/p4_agent.md`.

Phase 5 (conversational context) built on the same branch, **not yet merged**: `/api/agent` now reads a bounded, user-scoped window of prior turns from `app.conversation_message` and sends it to the service alongside the question (the browser never supplies history); `agent/context.py` re-clamps that window on arrival and renders a node-specific view of it, so `classify` sees the conversation's arc, `draft_sql` additionally gets the last successful query to transform, and `critic`/`summarize` get only the last exchange. A pending clarification is detected from message order alone (no mutable "resolved" column) and folded into one resumable task before `classify` runs — the graph topology, the single SQL execution path, and the Phase 4 response envelope are all unchanged. A separate multi-turn eval suite (`evals/conversation-v0.yaml`, 12 conversations / 26 turns, every gold query verified against live `sandbox_ro`) reports follow-up accuracy and whole-conversation success apart from first-turn accuracy. Measured live on 2026-08-09 against `claude-sonnet-5` (`evals/runs/`): **92.9% follow-up execution accuracy, 91.7% whole-conversation success, 100% first-turn accuracy and 100% outcome accuracy**, against a no-context control on the identical suite at **28.6% follow-up accuracy and 43.5% false abstention** — same first-turn accuracy, so the gap is context and nothing else. Carrying one exchange costs roughly +30% input tokens and +1.9s p50, with cache hit staying above 90%. The single miss is a column-projection disagreement with the case file, not a context failure, and is deliberately left unfixed pending human review of the case set — see `.agents/p5_conversational_context.md`.

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

Agent service + eval harness (Phase 4), from repo root:
```
.venv/bin/pip install -r agent/requirements.txt -r evals/requirements.txt

# Both suites are offline: no API key, no database, safe in CI.
.venv/bin/python -m pytest agent/tests evals/tests

# Run the service locally (needs ANTHROPIC_API_KEY, AGENT_SERVICE_TOKEN, and
# AGENT_API_BASE_URL pointing at a running Next app):
.venv/bin/python -m uvicorn agent.service:app --port 8080

# Eval run — COSTS MONEY (~$0.11), hits live data, never in CI:
.venv/bin/python -m evals.run --agent baseline
```

The root `Makefile` is the stable entry point for all of the above (Phase 5). Paid and
live work is deliberately excluded from `test` and `verify` — never add either as a
dependency of them:

| Command | Scope |
|---|---|
| `make test` | All offline/self-contained Python and web suites. |
| `make test-agent` / `test-pipeline` / `test-web` | The individual suites. |
| `make integration` | External disposable-Postgres grant tests; needs `BBALL_TEST_ADMIN_DSN`. |
| `make verify` | `make test` plus typecheck, lint, and production build. |
| `make eval` | **Paid/live.** Single-turn suite; `EVAL_AGENT=graph` by default. |
| `make eval-baseline` | **Paid/live.** Zero-shot control. |
| `make eval-conversation` | **Paid/live.** Multi-turn suite (`conversation-v0.yaml`). |
| `make eval-conversation-control` | **Paid/live.** Same suite with no context — the control. |

## File organization
- `db/migrations/NNNN_name.sql` — versioned SQL migrations, applied by hand via Supabase MCP (`apply_migration`), reviewed before applying. `0001` creates schema `nba` (`pbp_event`, `shot_detail`); `0002` creates the `sandbox_ro` role and its grants; `0003` creates schema `app` (NextAuth adapter tables, `query_log`) and the `app_rw` role (table-scoped DML on `app` only, `query_log` append-only).
- `web/` — the Next.js app. `web/lib/` holds the safety chain as separate tested modules: `validate-sql.ts` (pure libpg-query AST validator — defense in depth, NOT the boundary), `execute-query.ts` (`sandbox_ro` execution wrapper + query logging), `rate-limit.ts` (sliding window over `app.query_log`), `require-session.ts` (the only auth gate); `web/app/api/query/route.ts` composes them, and `web/app/api/schema|history/route.ts` are gated reads that never touch the executor/logger/rate limiter. `web/lib/test-cluster.ts` is the vitest globalSetup that builds the disposable Postgres (applies all of `db/migrations/`). `web/lib/api-types.ts` is the client/server response contract; `web/lib/examples.ts` the verified example queries. `web/components/` holds the sandbox UI: `Sandbox.tsx` (the one client component that owns state + all fetches) composing pure props-in/callbacks-out children (`SqlEditor`, `SchemaBrowser`, `ExampleList`, `HistoryPanel`, `ResultsArea` → `TableView`); components style via semantic tokens only and never import server-only modules (pools/executor) — that's the server/client boundary, enforce in review.
- `db/tests/` — pytest/psycopg2 suite asserting on `sandbox_ro`'s actual grants (not application behavior) against a disposable local Postgres. Lives next to `db/migrations/` rather than the repo-root `tests/` since it's testing the data layer, not the pipeline.
- `pipeline/` — the Python ETL package (config, download, transform, load, CLI). `pipeline/seasons.yaml` is the season-coverage config; editing it and rerunning is the entire backfill mechanism, no code changes.
- `tests/` — pytest suite for the ETL pipeline (parse/clean edge cases, load/idempotency), fixtures built from real downloaded sample data, not invented.
- `agent/` — the deployed Python service (FastAPI + LangGraph). `graph.py` owns the edges and bounded-retry caps; `nodes/` one module per node; `execute.py` reaches SQL ONLY via HTTP to `/api/internal/query`; `envelope.py` is the single home for the agent's result types; `context.py` (Phase 5) is the conversation-context contract — its bounds are enforced in the Pydantic validator, so an oversized window is clamped on arrival rather than trusted as sent, and its per-node renderings are the one place that decides what history each node sees. Its TypeScript half is `web/lib/conversation-context.ts`; the two must agree field for field. **`agent/` must never import `evals/`** — the container ships only `agent/`, and the dependency runs the other way (the harness measures the agent). `agent/tests/test_no_eval_dependency.py` enforces this, along with the invariant that the service imports no Postgres driver: it holds an LLM key and a service token, never a DSN.
- `evals/` — the eval harness. `harness/comparator.py` is the definition of "correct" (execution accuracy: ignore column names, ignore row order unless `order_matters`, multiset semantics, 1e-6 float tolerance) and is unit-tested on synthetic fixtures so it runs in CI. `harness/runner.py` and `run.py` need live data and an API key, cost money, and must NEVER run in CI. `text2sql-v0.yaml` is the single-turn case set; `conversation-v0.yaml` (Phase 5) is the multi-turn one, loaded/run/scored/reported by the four `harness/conversation_*.py` modules — kept separate so `text2sql-v0.yaml` stays byte-identical and directly comparable with the Phase 4 baseline. `runs/` holds stored runs so results can be diffed across changes.
- `.agents/` — gitignored scratch space; each phase's investigation/design decision logs live here (e.g. `p0_source_investigation.md`, `p0_schema_design.md`, `p0_sandbox_ro_design.md`, `p0_etl_design.md`) as reasoning context for later tasks, not as product docs.

## Verification
A task touching the web app isn't done until `npm test` (from `web/`), `npx tsc --noEmit`, and `npm run build` all pass. Anything touching the safety chain (validator, executor, rate limit, session gate, or the route composing them) must keep the route integration tests and the executor's three-together assertions (timeout / row cap / log) green — those tests are the enforcement of the critical rules above, not a formality.

A task touching the data layer isn't done until:
1. `.venv/bin/python -m pytest tests/` passes (ETL parse/clean/load/idempotency).
2. `BBALL_TEST_ADMIN_DSN=... .venv/bin/pytest db/tests` passes (sandbox_ro grants) — mandatory for any change to `db/migrations/0002_sandbox_ro_role.sql`, since this suite is the definition of "correctly restricted," not a formality.
3. For a schema or grants change: a migration file exists, was reviewed, and was applied to the live Supabase project via `apply_migration` (never applied ad hoc without a corresponding file in `db/migrations/`).

## Open decisions
- Phase 1 decisions (OAuth: GitHub only; rate limiting: Postgres-based over `app.query_log`; sessions: database strategy for server-side revocation — reasoning in `web/auth.ts`) all resolved.
- Supavisor pooler confirmed (2026-08-03): transaction mode at `aws-0-ca-central-1.pooler.supabase.com:6543`, username `<role>.<project_ref>` (works for both `sandbox_ro` and `app_rw`). Vercel envs use the pooled DSNs; local dev uses the direct connection.
- Vercel project `bball-oracle` (personal scope, rootDirectory `web`, deployment protection off) is linked; production at `https://bball-oracle.vercel.app`. Two GitHub OAuth apps exist by design: dev (localhost callback, creds in `web/.env.local`) and prod (creds in Vercel env). Git-integration auto-deploys are NOT wired yet — `vercel git connect` returns repo_not_found despite the GitHub App being installed; deploys are CLI-driven (`npx vercel deploy [--prod]` from repo root) until the repo is connected via the Vercel dashboard (project → Settings → Git).

## Resolved during Phase 0
- **Season window:** 2023-24 regular season only, loaded into the live Supabase project (567,662 `pbp_event` rows, 218,701 `shot_detail` rows). Measured at 216MB for one regular season (no playoffs) — the originally proposed 5-season regular+playoffs window would exceed 1GB. Decision: validate the MVP on one season on free tier first; expand via `pipeline/seasons.yaml` + Supabase Pro upgrade once validated, not before. See `project_plan.md` §3/§10.
- **Supabase tier/budget:** confirmed free tier, by design scoped to the one-season window above.
- Supabase API "exposed schemas" setting — not independently verified that `nba` is excluded from PostgREST exposure. `get_advisors` flagged RLS-disabled on both new tables (expected, since the app reaches them only via a direct `sandbox_ro` Postgres connection, never PostgREST) but that's a design assumption, not a verified setting. Worth a manual dashboard check (Settings → API → Exposed schemas) before Phase 2.
- Supavisor pooler username format for `sandbox_ro` (likely `sandbox_ro.<project_ref>`, per `.agents/p0_sandbox_ro_design.md` §6) and the right `CONNECTION LIMIT` value (currently `10`, a guess) — resolve when wiring Phase 1's connection string.

## Common pitfalls
None logged yet. Add an entry here only after the same mistake happens twice (two-strikes rule) — don't pre-populate with hypothetical failure modes.
