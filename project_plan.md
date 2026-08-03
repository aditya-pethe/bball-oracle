# bball-oracle — Project Plan / PRD

Status: MVP planning
Owner: Aditya Pethe

This document is the source of truth for scope and architecture. Other agents/contributors should treat it as the spec — if you deviate from it, update it.

## 1. Vision

A hosted basketball analytics app where users sign in and run SQL directly against a real NBA play-by-play dataset, getting results back in a clean UI. MVP is deliberately narrow: prove that "sign in → write SQL → query real NBA data → see results" works end-to-end, safely and cheaply. Natural-language querying, visualizations, and growth features come later.

## 2. Goals / Non-Goals (MVP)

**Goals:**
- Authenticated users can browse table schema and write arbitrary read-only SQL against a hosted NBA dataset.
- Query execution is safe (no mutation, no runaway queries, no DoS) and cheap enough to run on free/low tiers.
- Data pipeline is repeatable: rerunning it to add seasons doesn't require rework.

**Non-goals (explicitly deferred):**
- Natural-language-to-SQL agent.
- Charts/visualization/report builder.
- Precomputed aggregate tables (player/team/season rollups).
- Full historical backfill (1996–present) — starting with recent seasons only.
- Public marketing site / growth features / billing.
- Multi-source data (cdnnba, datanba, nbastatsv3, pbpstats, matchups) — revisit if `nbastats` + `shotdetail` prove insufficient.

## 3. Data Source

**Source:** [`shufinskiy/nba_data`](https://github.com/shufinskiy/nba_data) (Apache-2.0 licensed *scripts*; underlying data is scraped from stats.nba.com/data.nba.com/cdn.nba.com, which have their own terms — see §9).

**Tables for MVP** (confirmed): the dataset has no box-score/season/dimension tables — everything is event grain. We're using two of the five available sources, since the other three are largely redundant with these:

| Table | Source key | Grain | Key fields | Notes |
|---|---|---|---|---|
| `pbp_event` | `nbastats` | one row per game event | `GAME_ID`, `EVENTNUM`, `EVENTMSGTYPE`, `PERIOD`, `PCTIMESTRING`, `HOMEDESCRIPTION`/`VISITORDESCRIPTION`, `SCORE`, `SCOREMARGIN`, `PLAYER1_ID`/`NAME`/`TEAM_ID`/`TEAM_ABBREVIATION` (+ PLAYER2/3 variants) | Classic stats.nba.com play-by-play. Richest documented fields, full history available (not all loaded in MVP — see below). |
| `shot_detail` | `shotdetail` | one row per shot attempt | `GAME_ID`, `GAME_EVENT_ID`, `PLAYER_ID`/`NAME`, `TEAM_ID`/`NAME`, `PERIOD`, `EVENT_TYPE`, `ACTION_TYPE`, `SHOT_TYPE`, `SHOT_ZONE_*`, `SHOT_DISTANCE`, `LOC_X`, `LOC_Y`, `SHOT_MADE_FLAG`, `GAME_DATE` | Adds shot location/zone data `pbp_event` lacks — enables shot-chart-style queries. |

No player/team dimension tables exist upstream; both are denormalized inline (name + ID + team on every row). A derived `player`/`team` lookup table (for schema-browser autocomplete, cleaner joins) is a cheap post-MVP add, not required to query.

**Season range for MVP:** started with **2023-24 regular season only** (loaded and confirmed in the live Supabase project as of Phase 0), not the originally proposed 5-season window. Reason: measuring actual size after loading one regular season showed **216MB for a single regular season alone** (`pbp_event` + `shot_detail`, no playoffs) — the full 5-season regular+playoffs window would land well over 1GB, past the free tier's 500MB cap. Decision: stay on free tier and validate the MVP against one season first; expand to the full 2020-21 through 2024-25 window (regular + playoffs) and upgrade to Supabase Pro only once the single-season MVP is validated. The pipeline is season-parameterized (`pipeline/seasons.yaml`) specifically so this expansion is a config change + rerun, not a rewrite — same mechanism intended for the eventual full historical backfill (back to 1996-97), which remains a post-MVP task.

**Refresh cadence:** none needed for MVP — completed historical seasons don't change. In-season refresh (current season data updates) is a post-MVP concern.

## 4. Architecture

```
User (browser)
   │
   ▼
Next.js app (Vercel)
 ├─ Frontend: sandbox UI, auth pages          (Next.js App Router, CSR for sandbox)
 └─ API routes: /api/query, /api/auth/*        (Node runtime)
        │
        ├─ NextAuth (session/auth)
        │
        └─ Postgres connection, restricted "sandbox_ro" role
                │
                ▼
        Supabase Postgres (hosted DB)
          - schema `nba`: pbp_event, shot_detail
          - schema `app`: users/sessions (NextAuth), query_log
                ▲
                │  (offline, not on request path)
        Python ETL pipeline
          - downloads nba_data tar.xz archives (nbastats, shotdetail)
          - parses/cleans, normalizes types
          - bulk loads into Supabase via COPY
          - run manually / on demand, not a deployed service
```

- **Frontend + backend:** single Next.js app on Vercel — API routes instead of a separate backend server, per original plan.
- **Auth:** NextAuth (Auth.js), full accounts from day 1 (not deferred) — required because the sandbox executes arbitrary SQL and needs per-user rate limiting/audit, not just IP-based controls. OAuth provider: **GitHub only** for MVP (decided at Phase 1 planning); Google is a config-only addition later if wanted.
- **Database:** Supabase Postgres. Direct Postgres connection from API routes (not PostgREST) — arbitrary SQL requires a real SQL connection, not a REST-over-tables layer.
- **Rendering:** CSR for the sandbox (matches original plan — interactive, session-gated, no SEO value). SSR/ISR deferred; static season pages are a post-MVP idea (§10), not core MVP.

## 5. Query Execution & Safety Design

This is the highest-risk part of the product — a public endpoint that runs user-submitted SQL against a real database. Defense in depth, in priority order:

1. **Auth gate:** `/api/query` requires a valid NextAuth session. No anonymous execution.
2. **Restricted DB role (primary boundary):** a dedicated `sandbox_ro` Postgres role, granted `SELECT` only on `nba.pbp_event` and `nba.shot_detail`. No access to `app` schema (users, sessions, query_log) or any other schema. This is the authoritative control — everything else is defense in depth, not the thing actually preventing damage.
3. **Statement-level validation (defense in depth):** parse the submitted SQL server-side (e.g. via a proper SQL parser, not regex) and reject: anything that isn't a single `SELECT` statement, multiple statements, and disallowed keywords (`INSERT`/`UPDATE`/`DELETE`/`DROP`/`ALTER`/`COPY`/`GRANT`/etc.).
4. **Execution wrapper:** run each query in its own transaction as the `sandbox_ro` role: `BEGIN READ ONLY; SET LOCAL statement_timeout = '5s'; <query>; ROLLBACK;`
5. **Row cap:** hard cap results at e.g. 1,000 rows (server-side, not client-truncated); surface a "results truncated" notice.
6. **Rate limiting:** per-user and per-IP, independent of the per-query timeout — caps total query volume, not just single-query cost. Implementation (decided at Phase 1 planning): Postgres-based sliding window over recent `app.query_log` rows — no new infrastructure at MVP scale; swap in Redis (e.g. Upstash) only if it becomes a measured bottleneck.
7. **Audit logging:** every query (text, user id, duration, row count, error/success) logged to `app.query_log` for abuse monitoring.
8. **Connection pooling:** capped pool size for the `sandbox_ro` role (Supabase's built-in pooler) so one abusive user can't exhaust connections.

## 6. Frontend

Pages/routes for MVP:

| Route | Purpose |
|---|---|
| `/` | Minimal landing page: what this is, sign-in CTA. Not a marketing site. |
| `/signin` | NextAuth sign-in flow. |
| `/sandbox` | Core product: schema browser (tables/columns for `pbp_event`, `shot_detail`), SQL editor, run button, results table, query history, example/starter queries. |

Sandbox components:
- **SQL editor:** CodeMirror 6 with SQL language mode (lighter weight than Monaco; sufficient for this use case).
- **Schema browser:** sidebar listing available tables + columns/types, so users aren't guessing at schema.
- **Results table:** paginated/virtualized for large result sets, clear messaging on truncation/timeout/errors.
- **Query history:** persisted per user (backed by `app.query_log` or a dedicated table) — also useful as an abuse audit trail (§5).
- **Example queries:** a small set of starter queries (e.g. "top scorers in a game," "made shots by zone for a player") — important since a blank SQL box against unfamiliar event-grain data is an empty-state problem.

## 7. Data Pipeline (Python)

- One-off/on-demand script(s), not a deployed service — historical data doesn't change on a request path.
- Steps: download `nbastats_<season>` and `shotdetail_<season>` `.tar.xz` archives from `nba_data` → extract/parse CSVs → clean & normalize types (dates, IDs, nullable fields) → bulk load into Supabase via `COPY`.
- Parameterized by season list, so expanding coverage (recent → full history) is a config change and a rerun, not a rewrite.
- Idempotent: safe to rerun per season without duplicating rows (upsert or truncate-and-reload per season partition).

## 8. Non-Functional Requirements

- Query timeout: 5–10s server-enforced.
- No formal uptime SLA (personal project) — basic error visibility via Vercel logs is sufficient for MVP; Sentry or similar is a stretch add.
- Single-region deployment is fine.

## 9. Data Licensing (acknowledged, not blocking)

The `nba_data` repo's Apache-2.0 license covers the maintainer's collection scripts, not the underlying NBA data itself (scraped from stats.nba.com/data.nba.com/cdn.nba.com, which have their own terms of use). Comparable projects (e.g. `nba_api`) draw the same distinction — client code openly licensed, underlying data governed separately by NBA.com's terms. Decision: proceeding without further legal gating for MVP. Lightweight mitigations already in the design (row caps, rate limiting, auth-gating) incidentally limit bulk redistribution risk. Revisit if this moves toward a commercial product (ads, paid tiers, reselling access).

## 10. Open Questions / Assumptions to Confirm

- **OAuth provider(s) for NextAuth:** resolved — GitHub only for MVP. See §4.
- **Exact season window:** resolved for now — 2023-24 regular season only, on free tier, pending MVP validation. See §3.
- **Supabase tier/budget:** resolved for now — confirmed free tier, deliberately scoped to one season because 5-season volume (measured at 216MB/regular-season, so 1GB+ for the full window) doesn't fit free tier. Upgrade to Pro before expanding the season window.
- **`nba-sql` sibling repo** (`../nba-sql`, mpope9's NBA-API-to-Postgres loader): currently treated as unrelated prior art, not reused — it sources from the live NBA API rather than `nba_data`, and predates this project. Flag if you actually want its loader/schema patterns reused.

## 11. Phased Roadmap

**Phase 0 — Data pipeline & schema**
Python ETL for `nbastats` + `shotdetail`, recent seasons; Postgres schema in Supabase; `sandbox_ro` role with SELECT-only grants.

**Phase 1 — Query execution API**
`/api/query`: auth-gated, parser-validated, executed via restricted role with timeout/row cap, rate-limited, logged.

**Phase 2 — Sandbox UI**
NextAuth sign-in, schema browser, CodeMirror editor, results table, query history, example queries.

**Phase 3 — Landing page & deploy**
Minimal landing page, attribution/disclaimer footer, deploy to Vercel production.

**Phase 4 — Post-MVP / stretch**
Full historical backfill; precomputed aggregate tables; derived player/team lookup tables; ISR static season pages; additional sources (`matchups`, `pbpstats`) if event-level analysis demands it; NL-to-SQL agent; visualization/report builder; broader user growth.

## 12. MVP Success Criteria

A signed-in user can: view the schema for `pbp_event` and `shot_detail`, write and run an arbitrary read-only SQL query against the loaded seasons, see results (or a clear error/timeout/truncation message) within a few seconds — while the system rejects mutations, enforces timeouts/row caps, and rate-limits/logs all query activity.
