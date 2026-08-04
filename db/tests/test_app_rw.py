"""What "correctly scoped" means for the app_rw role.

app_rw is the application's own connection (NextAuth adapter, query_log writes, rate-limit
reads). It is NOT a security boundary for user SQL -- that is sandbox_ro -- but its grants
bound the blast radius of an app-connection compromise: DML on the granted `app` tables
only, no DDL anywhere, and no access to `nba` at all.
"""

import psycopg2
import pytest
from psycopg2 import errorcodes

from conftest import APP_RW_ROLE, LATE_APP_TABLE
from test_sandbox_ro import NBA_TABLES, denied

AUTH_TABLES = ["app.users", "app.accounts", "app.sessions", "app.verification_token"]


# ---------------------------------------------------------------------------
# The role can do what the app needs.
# ---------------------------------------------------------------------------


def test_role_can_log_in(rw):
    with rw.cursor() as cur:
        cur.execute("SELECT current_user")
        assert cur.fetchone()[0] == APP_RW_ROLE


def test_full_dml_on_auth_tables(rw):
    """One pass through the NextAuth adapter's lifecycle: create, read, update, delete."""
    with rw.cursor() as cur:
        cur.execute(
            "INSERT INTO app.users (name, email) VALUES ('t', 't@example.com') RETURNING id"
        )
        user_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO app.sessions (\"userId\", expires, \"sessionToken\") "
            "VALUES (%s, now() + interval '1 day', 'tok') RETURNING id",
            (user_id,),
        )
        cur.execute("UPDATE app.users SET name = 't2' WHERE id = %s", (user_id,))
        cur.execute("SELECT name FROM app.users WHERE id = %s", (user_id,))
        assert cur.fetchone()[0] == "t2"
        cur.execute("DELETE FROM app.users WHERE id = %s", (user_id,))
        cur.execute("SELECT count(*) FROM app.sessions WHERE \"userId\" = %s", (user_id,))
        assert cur.fetchone()[0] == 0, "ON DELETE CASCADE did not clear the session"


def test_can_insert_and_read_query_log(rw):
    with rw.cursor() as cur:
        cur.execute(
            "INSERT INTO app.query_log (user_id, query_text, status, error_text) "
            "VALUES (NULL, 'DELETE FROM nba.pbp_event', 'validation_rejected', 'not a SELECT') "
            "RETURNING id",
        )
        log_id = cur.fetchone()[0]
        cur.execute("SELECT status FROM app.query_log WHERE id = %s", (log_id,))
        assert cur.fetchone()[0] == "validation_rejected"


def test_unqualified_names_resolve_to_app(rw):
    """@auth/pg-adapter issues unqualified table names; the role's search_path covers it."""
    with rw.cursor() as cur:
        cur.execute("SELECT count(*) FROM users")


# ---------------------------------------------------------------------------
# The audit log is append-only for the app.
# ---------------------------------------------------------------------------


def test_query_log_update_is_denied(rw):
    denied(rw, "UPDATE app.query_log SET query_text = 'scrubbed'")


def test_query_log_delete_is_denied(rw):
    denied(rw, "DELETE FROM app.query_log")


def test_query_log_truncate_is_denied(rw):
    denied(rw, "TRUNCATE app.query_log")


# ---------------------------------------------------------------------------
# No access to `nba` -- user data is reachable only through sandbox_ro.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", NBA_TABLES)
def test_nba_reads_are_denied(rw, table):
    denied(rw, f"SELECT count(*) FROM {table}")


@pytest.mark.parametrize("table", NBA_TABLES)
def test_nba_writes_are_denied(rw, table):
    denied(rw, f"DELETE FROM {table}")


# ---------------------------------------------------------------------------
# No DDL, no escalation, table-scoped grants.
# ---------------------------------------------------------------------------


def test_table_added_to_app_after_the_grants_is_not_readable(rw):
    denied(rw, f"SELECT * FROM {LATE_APP_TABLE}")


@pytest.mark.parametrize("schema", ["app", "nba", "public"])
def test_cannot_create_tables_in_any_schema(rw, schema):
    denied(rw, f"CREATE TABLE {schema}.injected (id integer)")


def test_cannot_alter_granted_tables(rw):
    denied(rw, "ALTER TABLE app.query_log ADD COLUMN injected integer")


def test_cannot_set_role_to_owner(rw, admin):
    with admin.cursor() as cur:
        cur.execute("SELECT current_user")
        owner = cur.fetchone()[0]
    denied(rw, f"SET ROLE {owner}")


def test_table_grants_are_exactly_the_granted_app_tables(admin):
    with admin.cursor() as cur:
        cur.execute(
            "SELECT n.nspname, c.relname, a.privilege_type "
            "FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace, "
            "LATERAL aclexplode(c.relacl) a "
            "WHERE a.grantee = %s::regrole AND c.relkind = 'r'",
            (APP_RW_ROLE,),
        )
        full_dml = {"SELECT", "INSERT", "UPDATE", "DELETE"}
        expected = {
            (table.split(".")[1], priv) for table in AUTH_TABLES for priv in full_dml
        } | {("query_log", "SELECT"), ("query_log", "INSERT")}
        assert {(rel, priv) for _, rel, priv in cur.fetchall()} == expected


def test_schema_grants_are_exactly_usage_on_app(admin):
    with admin.cursor() as cur:
        cur.execute(
            "SELECT n.nspname, a.privilege_type "
            "FROM pg_namespace n, LATERAL aclexplode(n.nspacl) a "
            "WHERE a.grantee = %s::regrole",
            (APP_RW_ROLE,),
        )
        assert set(cur.fetchall()) == {("app", "USAGE")}


def test_no_default_privileges_target_the_role(admin):
    with admin.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_default_acl d, LATERAL aclexplode(d.defaclacl) a "
            "WHERE a.grantee = %s::regrole",
            (APP_RW_ROLE,),
        )
        assert cur.fetchone()[0] == 0


def test_role_attributes_are_minimal(admin):
    with admin.cursor() as cur:
        cur.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, "
            "       rolinherit, rolcanlogin, rolconnlimit "
            "FROM pg_roles WHERE rolname = %s",
            (APP_RW_ROLE,),
        )
        row = cur.fetchone()
    assert row is not None, "app_rw role does not exist"
    rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolinherit, \
        rolcanlogin, rolconnlimit = row
    assert not rolsuper
    assert not rolcreatedb
    assert not rolcreaterole
    assert not rolreplication
    assert not rolbypassrls
    assert not rolinherit
    assert rolcanlogin
    assert 0 < rolconnlimit <= 20


def test_role_has_no_group_memberships(admin):
    with admin.cursor() as cur:
        cur.execute(
            "SELECT r.rolname FROM pg_auth_members m "
            "JOIN pg_roles r ON r.oid = m.roleid "
            "WHERE m.member = %s::regrole",
            (APP_RW_ROLE,),
        )
        assert cur.fetchall() == []


# ---------------------------------------------------------------------------
# The two roles stay disjoint: sandbox_ro must not see what app_rw can.
# ---------------------------------------------------------------------------


def test_sandbox_ro_cannot_read_the_real_auth_tables(ro):
    """0003 created the real app schema; re-pin isolation against every real table."""
    for table in AUTH_TABLES + ["app.query_log"]:
        error = denied(ro, f"SELECT * FROM {table}")
        assert "app" in str(error)
