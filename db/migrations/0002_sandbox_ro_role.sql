-- 0002_sandbox_ro_role.sql
-- Phase 0: the `sandbox_ro` role.
--
-- This is THE security boundary for user-submitted SQL (AGENTS.md, project_plan.md §5.2).
-- The application's SQL parser is defense in depth only; if this role is wrong, nothing
-- else in the chain is load-bearing. Verified by db/tests/test_sandbox_ro.py -- change the
-- grants here and run that suite, never the other way round.
--
-- Deliberately NOT here: a password. The role ships LOGIN with a NULL password, which under
-- Supabase's scram-only pg_hba means it cannot authenticate until a credential is
-- provisioned out of band:
--     ALTER ROLE sandbox_ro PASSWORD '<from the secret store>';
-- Committing a credential to the migration history would put it in every clone forever.

BEGIN;

-- ---------------------------------------------------------------------------
-- The role
--
-- LOGIN, with its own connection string, rather than NOLOGIN + `SET ROLE` from the app's
-- privileged connection. `SET ROLE` is reversible by whoever issued it -- a single
-- `RESET ROLE` reaching the server would return the session to full privilege, which would
-- make the application's statement filter the real boundary. AGENTS.md forbids exactly that.
--
-- NOINHERIT so that adding sandbox_ro to a group role later does not silently widen it.
-- CONNECTION LIMIT backstops project_plan.md §5.8: the pooler is meant to cap this, but a
-- pooler misconfiguration should not become a cluster-wide connection exhaustion.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sandbox_ro') THEN
        CREATE ROLE sandbox_ro
            LOGIN
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOINHERIT
            NOREPLICATION
            NOBYPASSRLS
            CONNECTION LIMIT 10;
    END IF;
END
$$;

-- Re-asserted so re-running this migration converges on the intended attributes.
-- SUPERUSER/REPLICATION/BYPASSRLS are omitted here on purpose: naming any of them in
-- ALTER ROLE requires a true superuser even when setting them to NO, and Supabase's
-- `postgres` role is not one. They are set once, at CREATE, where NO-values are permitted
-- to a plain CREATEROLE role. The test suite asserts all three are off regardless.
ALTER ROLE sandbox_ro
    LOGIN
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    CONNECTION LIMIT 10;

COMMENT ON ROLE sandbox_ro IS
  'Query-sandbox execution role. SELECT on nba.pbp_event and nba.shot_detail only; no access to app or any other schema. Grants are table-scoped by design -- a new table in nba is NOT exposed until granted here explicitly. See db/tests/test_sandbox_ro.py.';


-- ---------------------------------------------------------------------------
-- Grants: table-scoped, never schema-wide.
--
-- `GRANT SELECT ON ALL TABLES IN SCHEMA nba` and `ALTER DEFAULT PRIVILEGES` are both
-- deliberately absent. A future table in `nba` (a derived player/team lookup, a staging
-- table left behind by the ETL) must be granted here explicitly and get its own line, so
-- exposure is always a reviewed decision rather than a side effect of CREATE TABLE.
-- ---------------------------------------------------------------------------
GRANT USAGE ON SCHEMA nba TO sandbox_ro;

GRANT SELECT ON TABLE nba.pbp_event  TO sandbox_ro;
GRANT SELECT ON TABLE nba.shot_detail TO sandbox_ro;


-- ---------------------------------------------------------------------------
-- Close the `public` schema.
--
-- Postgres <15 grants CREATE on schema `public` to PUBLIC, i.e. to sandbox_ro. That is a
-- write primitive (and a disk-fill DoS) outside the `nba` schema, so it has to go. A
-- privilege that reaches a role through PUBLIC can only be revoked from PUBLIC, so this is
-- necessarily global -- it affects only roles relying on the implicit grant (Supabase grants
-- anon/authenticated/service_role explicitly, so they are unaffected).
--
-- Guarded rather than unconditional: on PG15+ the grant is already absent and revoking is a
-- pointless global change. If the hole IS present and this migration is not being applied by
-- the owner of schema `public`, it aborts loudly rather than leaving sandbox_ro with a write
-- primitive -- a silently-skipped security revoke is the failure mode worth avoiding here.
-- ---------------------------------------------------------------------------
-- The outcome is re-checked rather than trusted: like GRANT, a REVOKE issued by a non-owner
-- does not raise -- it emits 'WARNING: no privileges could be revoked' and changes nothing.
-- Catching insufficient_privilege here would never fire.
DO $$
BEGIN
    IF has_schema_privilege('sandbox_ro', 'public', 'CREATE') THEN
        REVOKE CREATE ON SCHEMA public FROM PUBLIC;

        IF has_schema_privilege('sandbox_ro', 'public', 'CREATE') THEN
            RAISE EXCEPTION
                'sandbox_ro can still CREATE in schema public; % is not the owner of that '
                'schema and the REVOKE was a no-op. Re-run as the owner of schema public.',
                current_user;
        END IF;
    END IF;
END
$$;


-- ---------------------------------------------------------------------------
-- Session defaults carried by the role.
--
-- Backstops, NOT the boundary: all four are USERSET, so a session can turn them off. They
-- exist so that any execution path which forgets project_plan.md §5.4's
-- `BEGIN READ ONLY; SET LOCAL statement_timeout` still gets a bounded, read-only session.
-- The app must keep setting its own per-query SET LOCAL -- these are the floor, not it.
--
-- statement_timeout is 10s (the top of project_plan.md §8's 5-10s range) so the app's
-- tighter 5s SET LOCAL stays the value that actually fires in normal operation, and this
-- one only shows up when something skipped the wrapper.
-- ---------------------------------------------------------------------------
ALTER ROLE sandbox_ro SET statement_timeout = '10s';
ALTER ROLE sandbox_ro SET idle_in_transaction_session_timeout = '15s';
ALTER ROLE sandbox_ro SET default_transaction_read_only = on;

-- Unqualified table names resolve inside `nba` and nowhere else; `public` is not searched.
ALTER ROLE sandbox_ro SET search_path = nba;

COMMIT;
