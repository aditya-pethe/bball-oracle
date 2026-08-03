"""What "correctly restricted" means for the sandbox_ro role.

Per AGENTS.md, this role -- not the application's SQL parser -- is the security boundary for
user-submitted SQL. Every deny-case below is asserted on a connection whose soft GUC
backstops (default_transaction_read_only, statement_timeout) have been switched off, so a
pass means the *privilege* is absent, not that a read-only transaction happened to block it.
"""

import psycopg2
import pytest
from psycopg2 import errorcodes

from conftest import LATE_TABLE, SANDBOX_ROLE

NBA_TABLES = ["nba.pbp_event", "nba.shot_detail"]

INSUFFICIENT_PRIVILEGE = errorcodes.INSUFFICIENT_PRIVILEGE  # 42501


def denied(conn, statement, *, code=INSUFFICIENT_PRIVILEGE):
    """Assert `statement` is rejected by Postgres, and return the error for inspection.

    Runs inside an explicit transaction block, mirroring project_plan.md §5.4's execution
    wrapper. This is not cosmetic: `LOCK TABLE` outside a transaction block is downgraded to
    a warning and never reaches its privilege check, so an autocommit-per-statement harness
    would report that lock as "denied" without having tested anything.
    """
    with pytest.raises(psycopg2.Error) as exc:
        with conn.cursor() as cur:
            try:
                cur.execute("BEGIN")
                cur.execute(statement)
            finally:
                cur.execute("ROLLBACK")
    assert exc.value.pgcode == code, (
        f"{statement!r} failed with {exc.value.pgcode} "
        f"({exc.value.pgerror.strip() if exc.value.pgerror else ''}), expected {code}"
    )
    return exc.value


# ---------------------------------------------------------------------------
# The role can do the one thing it exists to do.
# ---------------------------------------------------------------------------


def test_role_can_log_in(ro):
    with ro.cursor() as cur:
        cur.execute("SELECT current_user")
        assert cur.fetchone()[0] == SANDBOX_ROLE


@pytest.mark.parametrize("table", NBA_TABLES)
def test_select_is_allowed(ro, table):
    with ro.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        assert cur.fetchone()[0] == 0


def test_join_across_both_tables_is_allowed(ro):
    with ro.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM nba.pbp_event p "
            "JOIN nba.shot_detail s "
            "  ON (p.game_id, p.eventnum) = (s.game_id, s.game_event_id)"
        )
        assert cur.fetchone()[0] == 0


def test_can_read_column_metadata_for_the_schema_browser(ro):
    with ro.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema = 'nba' AND table_name = 'shot_detail'"
        )
        assert cur.fetchone()[0] > 0


# ---------------------------------------------------------------------------
# Mutation / DDL / privilege-escalation on the two granted tables.
# ---------------------------------------------------------------------------

MUTATIONS = {
    "insert": "INSERT INTO {t} SELECT * FROM {t}",
    "update": "UPDATE {t} SET season = season",
    "delete": "DELETE FROM {t}",
    "truncate": "TRUNCATE {t}",
    "drop": "DROP TABLE {t}",
    "alter_add_column": "ALTER TABLE {t} ADD COLUMN injected integer",
    "alter_owner": f"ALTER TABLE {{t}} OWNER TO {SANDBOX_ROLE}",
    "create_index": "CREATE INDEX injected_idx ON {t} (season)",
    "comment": "COMMENT ON TABLE {t} IS 'injected'",
    "copy_from_file": "COPY {t} FROM '/etc/passwd'",
    "select_for_update": "SELECT * FROM {t} FOR UPDATE",
    "lock_exclusive": "LOCK TABLE {t} IN ACCESS EXCLUSIVE MODE",
    "create_rule": "CREATE RULE injected AS ON SELECT TO {t} DO INSTEAD NOTHING",
    "create_trigger": (
        "CREATE TRIGGER injected AFTER INSERT ON {t} "
        "FOR EACH ROW EXECUTE FUNCTION suppress_redundant_updates_trigger()"
    ),
}


@pytest.mark.parametrize("table", NBA_TABLES)
@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_write_and_ddl_are_denied(ro, table, name):
    denied(ro, MUTATIONS[name].format(t=table))


@pytest.mark.parametrize("table", NBA_TABLES)
def test_grant_by_the_sandbox_role_changes_nothing(ro, admin, table):
    """GRANT from a non-owner is a *warning*, not an error -- assert the ACL, not the raise.

    Postgres lets a role without GRANT OPTION issue GRANT: it emits
    'WARNING: no privileges were granted' and silently does nothing. A test that only
    asserted an exception here would pass for the wrong reason.
    """
    schema, name = table.split(".")
    with admin.cursor() as cur:
        cur.execute("SELECT relacl::text FROM pg_class WHERE oid = %s::regclass", (table,))
        before = cur.fetchone()[0]

    with ro.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute(f"GRANT ALL ON {table} TO PUBLIC")
        cur.execute(f"GRANT ALL ON {table} TO {SANDBOX_ROLE} WITH GRANT OPTION")
        cur.execute("COMMIT")
    assert any("no privileges were granted" in n for n in ro.notices), ro.notices

    with admin.cursor() as cur:
        cur.execute("SELECT relacl::text FROM pg_class WHERE oid = %s::regclass", (table,))
        assert cur.fetchone()[0] == before, f"sandbox_ro widened the ACL on {schema}.{name}"


def test_server_side_file_write_is_denied(ro):
    denied(ro, "COPY (SELECT 1) TO '/tmp/sandbox_ro_exfil.csv'")


def test_server_side_file_read_is_denied(ro):
    denied(ro, "SELECT pg_read_file('/etc/passwd')")


# ---------------------------------------------------------------------------
# Cross-schema isolation. `app` here stands in for the real NextAuth/query_log schema.
# ---------------------------------------------------------------------------

APP_STATEMENTS = [
    "SELECT * FROM app.users",
    "SELECT count(*) FROM app.users",
    "INSERT INTO app.users VALUES (2, 'attacker@example.com')",
    "UPDATE app.users SET email = 'attacker@example.com'",
    "DELETE FROM app.users",
    "DROP TABLE app.users",
    "CREATE TABLE app.injected (id integer)",
]


@pytest.mark.parametrize("statement", APP_STATEMENTS)
def test_app_schema_is_unreachable(ro, statement):
    error = denied(ro, statement)
    assert "app" in str(error)


def test_app_schema_stays_invisible_even_via_search_path(ro):
    """Putting `app` on the search_path does not make it resolvable.

    Postgres drops schemas the role lacks USAGE on from the effective search path, so the
    table is not merely forbidden, it is invisible -- hence undefined_table, not
    insufficient_privilege.
    """
    with ro.cursor() as cur:
        cur.execute("SET search_path = app, nba")
    denied(ro, "SELECT * FROM users", code=errorcodes.UNDEFINED_TABLE)


def test_cannot_read_shared_auth_catalog(ro):
    denied(ro, "SELECT rolpassword FROM pg_authid")


def test_catalog_leaks_app_object_names_but_nothing_else(ro):
    """Known and accepted: pg_catalog is world-readable, so `app` TABLE NAMES are visible.

    Postgres has no way to hide them short of a custom catalog. Nothing about the columns or
    contents is reachable -- even resolving app.users to an OID needs USAGE on the schema.
    Pinned as a test so that if this ever widens, it widens loudly.
    """
    with ro.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'app'")
        assert cur.fetchone()[0] >= 1
    denied(ro, "SELECT attname FROM pg_attribute WHERE attrelid = 'app.users'::regclass")


# ---------------------------------------------------------------------------
# Grants are table-scoped: a table added to `nba` later must NOT be exposed.
# ---------------------------------------------------------------------------


def test_table_added_to_nba_after_the_grants_is_not_readable(ro):
    denied(ro, f"SELECT * FROM {LATE_TABLE}")


def test_no_default_privileges_target_the_role(admin):
    with admin.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_default_acl d, LATERAL aclexplode(d.defaclacl) a "
            "WHERE a.grantee = %s::regrole",
            (SANDBOX_ROLE,),
        )
        assert cur.fetchone()[0] == 0, (
            "ALTER DEFAULT PRIVILEGES targets sandbox_ro -- future tables would be "
            "auto-granted, defeating the table-scoped grant"
        )


def test_table_grants_are_exactly_the_two_nba_tables(admin):
    with admin.cursor() as cur:
        cur.execute(
            "SELECT n.nspname, c.relname, a.privilege_type "
            "FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace, "
            "LATERAL aclexplode(c.relacl) a "
            "WHERE a.grantee = %s::regrole",
            (SANDBOX_ROLE,),
        )
        assert set(cur.fetchall()) == {
            ("nba", "pbp_event", "SELECT"),
            ("nba", "shot_detail", "SELECT"),
        }


def test_schema_grants_are_exactly_usage_on_nba(admin):
    with admin.cursor() as cur:
        cur.execute(
            "SELECT n.nspname, a.privilege_type "
            "FROM pg_namespace n, LATERAL aclexplode(n.nspacl) a "
            "WHERE a.grantee = %s::regrole",
            (SANDBOX_ROLE,),
        )
        assert set(cur.fetchall()) == {("nba", "USAGE")}


def test_no_function_or_database_grants(admin):
    with admin.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_proc p, LATERAL aclexplode(p.proacl) a "
            "WHERE a.grantee = %s::regrole",
            (SANDBOX_ROLE,),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT count(*) FROM pg_database d, LATERAL aclexplode(d.datacl) a "
            "WHERE a.grantee = %s::regrole",
            (SANDBOX_ROLE,),
        )
        assert cur.fetchone()[0] == 0


# ---------------------------------------------------------------------------
# The role cannot create anything, anywhere, or escalate itself.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("schema", ["nba", "public", "app"])
def test_cannot_create_tables_in_any_schema(ro, schema):
    denied(ro, f"CREATE TABLE {schema}.injected (id integer)")


def test_cannot_create_schema(ro):
    denied(ro, "CREATE SCHEMA injected")


def test_cannot_create_roles(ro):
    denied(ro, "CREATE ROLE injected LOGIN")


def test_cannot_grant_itself_superuser(ro):
    with pytest.raises(psycopg2.Error):
        with ro.cursor() as cur:
            cur.execute(f"ALTER ROLE {SANDBOX_ROLE} SUPERUSER")


def test_cannot_set_role_to_a_privileged_role(ro, admin):
    with admin.cursor() as cur:
        cur.execute("SELECT current_user")
        owner = cur.fetchone()[0]
    denied(ro, f"SET ROLE {owner}")


def test_role_attributes_are_minimal(admin):
    with admin.cursor() as cur:
        cur.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, "
            "       rolinherit, rolcanlogin, rolconnlimit "
            "FROM pg_roles WHERE rolname = %s",
            (SANDBOX_ROLE,),
        )
        row = cur.fetchone()
    assert row is not None, "sandbox_ro role does not exist"
    (
        rolsuper,
        rolcreatedb,
        rolcreaterole,
        rolreplication,
        rolbypassrls,
        rolinherit,
        rolcanlogin,
        rolconnlimit,
    ) = row
    assert not rolsuper
    assert not rolcreatedb
    assert not rolcreaterole
    assert not rolreplication
    assert not rolbypassrls
    assert not rolinherit
    assert rolcanlogin, "the app connects as sandbox_ro directly; see design log"
    assert 0 < rolconnlimit <= 20, f"expected a capped connection limit, got {rolconnlimit}"


def test_role_has_no_group_memberships(admin):
    with admin.cursor() as cur:
        cur.execute(
            "SELECT r.rolname FROM pg_auth_members m "
            "JOIN pg_roles r ON r.oid = m.roleid "
            "WHERE m.member = %s::regrole",
            (SANDBOX_ROLE,),
        )
        assert cur.fetchall() == [], "sandbox_ro inherits privileges from a group role"


# ---------------------------------------------------------------------------
# Session defaults carried by the role itself (backstops, not the boundary).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "setting,expected",
    [
        ("statement_timeout", "10s"),
        ("default_transaction_read_only", "on"),
        ("idle_in_transaction_session_timeout", "15s"),
        ("search_path", "nba"),
    ],
)
def test_role_session_defaults(ro_defaults, setting, expected):
    with ro_defaults.cursor() as cur:
        cur.execute(f"SHOW {setting}")
        assert cur.fetchone()[0] == expected


def test_temp_tables_are_blocked_by_read_only_not_by_privilege(ro, ro_defaults):
    """Residual, accepted: TEMPORARY on the database is granted to PUBLIC.

    Revoking it would hit every role on the cluster, so it stays. Under the shipped defaults
    -- and under project_plan.md §5.4's `BEGIN READ ONLY` -- temp tables are unreachable.
    Both halves are pinned here so the tradeoff cannot quietly become something else.
    """
    denied(
        ro_defaults,
        "CREATE TEMP TABLE injected AS SELECT 1",
        code=errorcodes.READ_ONLY_SQL_TRANSACTION,
    )
    with ro.cursor() as cur:
        cur.execute("CREATE TEMP TABLE injected AS SELECT 1")


def test_app_still_gets_its_own_statement_timeout(ro_defaults):
    """The app sets a tighter SET LOCAL timeout per query (project_plan.md §5.4)."""
    with ro_defaults.cursor() as cur:
        cur.execute("BEGIN READ ONLY")
        cur.execute("SET LOCAL statement_timeout = '200ms'")
        with pytest.raises(psycopg2.Error) as exc:
            cur.execute("SELECT pg_sleep(2)")
        assert exc.value.pgcode == errorcodes.QUERY_CANCELED
    ro_defaults.rollback()
