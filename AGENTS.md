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
Pre-implementation. No application code, commands, or directory structure exist yet — Phase 0 (data pipeline) has not started. Update the sections below the moment they stop being true; don't let this file describe an aspirational state.

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
Not yet established — no scaffolding exists. Add copy-pasteable build/test/lint commands here as soon as the app and pipeline are scaffolded (Phase 0/1). Until this section is filled in, don't assume a command exists — check the repo.

## File organization
Not yet established beyond one thing: `.agents/` is scratch space for agent-generated tooling, scripts, and artifacts made while contributing. It's gitignored — nothing the app or pipeline depends on may live there. Add the full directory map here once Phase 0 scaffolding lands.

## Verification
Not yet established. Once tests exist, document the exact command(s) required before a task can be called done.

## Open decisions blocking Phase 0/2
- OAuth provider(s) for NextAuth — unconfirmed, currently assumed GitHub + Google.
- Exact season window — unconfirmed, currently proposed 2020-21 through 2024-25.
- Supabase tier/budget — project created (`project_ref=zblvjxuaqhjlnuprgemx`), tier/budget still unconfirmed.

## Common pitfalls
None logged yet. Add an entry here only after the same mistake happens twice (two-strikes rule) — don't pre-populate with hypothetical failure modes.
