"""Fixtures for the sandbox_ro grant tests.

Runs against a DISPOSABLE LOCAL Postgres only. The fixtures create and drop a database
and drop the cluster-global `sandbox_ro` role on teardown, so pointing this at a shared or
hosted instance (Supabase) would be destructive -- `_assert_disposable_target` refuses.

    createdb-capable local Postgres, then:
    BBALL_TEST_ADMIN_DSN=postgresql://postgres@127.0.0.1:5432/postgres \
        pytest db/tests -v
"""

import os
import pathlib
import re
import warnings

import psycopg2
import psycopg2.extensions
import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = pathlib.Path(
    os.environ.get("BBALL_MIGRATIONS_DIR", REPO_ROOT / "db" / "migrations")
)
ADMIN_DSN = os.environ.get(
    "BBALL_TEST_ADMIN_DSN", "postgresql://postgres@127.0.0.1:5432/postgres"
)

TEST_DB = f"bball_sandbox_ro_test_{os.getpid()}"
SANDBOX_ROLE = "sandbox_ro"
APP_RW_ROLE = "app_rw"
# Stands in for Supabase's `postgres`: owns the database, has CREATEROLE, is NOT a superuser.
OWNER_ROLE = "bball_test_owner"
OWNER_PASSWORD = "bball_test_owner_local_only"
# Local-cluster-only credentials. The migrations deliberately ship no passwords; see
# .agents/p0_sandbox_ro_design.md.
SANDBOX_PASSWORD = "sandbox_ro_local_test_only"
APP_RW_PASSWORD = "app_rw_local_test_only"

LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1", "/tmp", "/var/run/postgresql"}

# Tables added AFTER the grants ran, to prove the grants are table-scoped and that no
# ALTER DEFAULT PRIVILEGES is in play.
LATE_TABLE = "nba.added_after_grants"
LATE_APP_TABLE = "app.added_after_grants"


def _assert_disposable_target(dsn: str) -> None:
    params = psycopg2.extensions.parse_dsn(dsn)
    host = params.get("host", "")
    if host not in LOCAL_HOSTS and not host.startswith("/"):
        raise RuntimeError(
            f"refusing to run destructive grant tests against non-local host {host!r}"
        )
    if re.search(r"supabase|pooler|amazonaws|rds\.", dsn, re.I):
        raise RuntimeError(f"refusing to run destructive grant tests against {dsn!r}")


def _connect(dsn: str, **kwargs):
    conn = psycopg2.connect(dsn, **kwargs)
    conn.autocommit = True
    return conn


def _dsn_for(dbname: str, *, user: str | None = None, password: str | None = None) -> str:
    params = psycopg2.extensions.parse_dsn(ADMIN_DSN)
    params["dbname"] = dbname
    if user is not None:
        params["user"] = user
    if password is not None:
        params["password"] = password
    return psycopg2.extensions.make_dsn(**params)


def migration_files() -> list[pathlib.Path]:
    files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    if not files:
        raise RuntimeError(f"no migrations found in {MIGRATIONS_DIR}")
    return files


@pytest.fixture(scope="session")
def test_db_dsn():
    """A throwaway database owned by a non-superuser, CREATEROLE role.

    Deliberately not owned by the local superuser: Supabase's `postgres` is NOT a superuser,
    and several plausible spellings of this migration (`ALTER ROLE ... NOSUPERUSER`,
    `REVOKE ... FROM PUBLIC` on a schema it does not own) apply fine as superuser and fail or
    silently no-op as `postgres`. Running the suite under the weaker role is what makes it a
    check on whether the migration is actually applicable in production.
    """
    _assert_disposable_target(ADMIN_DSN)
    root = _connect(ADMIN_DSN)
    with root.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
        cur.execute(f"DROP ROLE IF EXISTS {OWNER_ROLE}")
        cur.execute(
            f"CREATE ROLE {OWNER_ROLE} LOGIN PASSWORD %s "
            f"NOSUPERUSER CREATEROLE NOREPLICATION NOBYPASSRLS",
            (OWNER_PASSWORD,),
        )
        cur.execute(f'CREATE DATABASE "{TEST_DB}" OWNER {OWNER_ROLE}')
    owner_dsn = _dsn_for(TEST_DB, user=OWNER_ROLE, password=OWNER_PASSWORD)

    # Supabase's `postgres` administers the public schema; the local template's copy is owned
    # by the bootstrap superuser, so hand it over to match.
    db_as_root = _connect(_dsn_for(TEST_DB))
    with db_as_root.cursor() as cur:
        cur.execute(f"ALTER SCHEMA public OWNER TO {OWNER_ROLE}")
    db_as_root.close()

    try:
        yield owner_dsn
    finally:
        with root.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
            # Roles are cluster-global, so the migration's CREATE ROLE would be skipped on
            # the next run if this one is left behind. Best-effort: the role may legitimately
            # own grants in another database on the same cluster.
            for role in (SANDBOX_ROLE, APP_RW_ROLE, OWNER_ROLE):
                try:
                    cur.execute(f"DROP ROLE IF EXISTS {role}")
                except psycopg2.Error as exc:
                    warnings.warn(f"could not drop {role}: {exc}")
        root.close()


@pytest.fixture(scope="session")
def admin(test_db_dsn):
    """Connection as the database owner, i.e. as whoever applies migrations in production."""
    conn = _connect(test_db_dsn)
    with conn.cursor() as cur:
        for path in migration_files():
            cur.execute(path.read_text())

        # The real `app` schema exists as of migration 0003; seed a victim row so the
        # sandbox_ro isolation tests assert against actual data, not an empty table.
        cur.execute(
            "INSERT INTO app.users (name, email) VALUES ('victim', 'victim@example.com')"
        )

        # Created after the migrations so it can only be visible via a schema-wide grant
        # or default privileges -- neither of which should exist.
        cur.execute(f"CREATE TABLE {LATE_TABLE} (id bigint PRIMARY KEY)")

        # A table added to `app` after the grants ran -- app_rw's grants are table-scoped
        # exactly like sandbox_ro's, so it must not be readable.
        cur.execute(f"CREATE TABLE {LATE_APP_TABLE} (id bigint PRIMARY KEY)")

        # Give both roles passwords for the duration of this run only. Raises if the
        # role migrations have not been applied -- the intended pre-migration failure.
        cur.execute(f"ALTER ROLE {SANDBOX_ROLE} PASSWORD %s", (SANDBOX_PASSWORD,))
        cur.execute(f"ALTER ROLE {APP_RW_ROLE} PASSWORD %s", (APP_RW_PASSWORD,))
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def sandbox_dsn(admin, test_db_dsn):
    return _dsn_for(TEST_DB, user=SANDBOX_ROLE, password=SANDBOX_PASSWORD)


@pytest.fixture
def ro_defaults(sandbox_dsn):
    """sandbox_ro connection with the role's own session defaults left intact."""
    conn = _connect(sandbox_dsn)
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def app_rw_dsn(admin, test_db_dsn):
    return _dsn_for(TEST_DB, user=APP_RW_ROLE, password=APP_RW_PASSWORD)


@pytest.fixture
def rw(app_rw_dsn):
    conn = _connect(app_rw_dsn)
    yield conn
    conn.close()


@pytest.fixture
def ro(sandbox_dsn):
    """sandbox_ro connection with the role's soft GUC backstops switched off.

    default_transaction_read_only and statement_timeout are user-settable, so a caller can
    turn them off. Tests use this connection so that a denial proves the *privilege* is
    missing, not merely that the transaction was read-only.
    """
    conn = _connect(sandbox_dsn)
    with conn.cursor() as cur:
        cur.execute("SET default_transaction_read_only = off")
        cur.execute("SET statement_timeout = 0")
    yield conn
    conn.close()
