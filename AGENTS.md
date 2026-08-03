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
- Supabase tier/budget — unconfirmed.

## Common pitfalls
None logged yet. Add an entry here only after the same mistake happens twice (two-strikes rule) — don't pre-populate with hypothetical failure modes.
