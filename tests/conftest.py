"""Session-scoped disposable local Postgres cluster for pipeline tests.

Spins up a throwaway cluster with `initdb`/`pg_ctl` (Homebrew Postgres 14, matching the
version the schema migration was validated against), listening only on a Unix socket in a
temp dir (no TCP, so it can't collide with a real Postgres on the default port), applies
db/migrations/0001_create_nba_schema.sql, and tears the whole thing down at the end of the
test session. Never touches Supabase or any network Postgres.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import psycopg2
import pytest

REPO_ROOT = Path(__file__).parent.parent
MIGRATION_PATH = REPO_ROOT / "db" / "migrations" / "0001_create_nba_schema.sql"


@pytest.fixture(scope="session")
def pg_cluster():
    tmp_dir = Path(tempfile.mkdtemp(prefix="bball_oracle_pg_"))
    data_dir = tmp_dir / "data"
    socket_dir = tmp_dir / "socket"
    socket_dir.mkdir(parents=True)

    subprocess.run(
        ["initdb", "-D", str(data_dir), "-U", "postgres", "-A", "trust", "--no-sync"],
        check=True,
        capture_output=True,
    )

    log_path = tmp_dir / "postgres.log"
    subprocess.run(
        [
            "pg_ctl", "-D", str(data_dir), "-l", str(log_path), "-w", "start",
            "-o", f"-c listen_addresses='' -c unix_socket_directories={socket_dir} -c fsync=off",
        ],
        check=True,
        capture_output=True,
    )

    try:
        # pg_ctl -w waits for the server to accept connections; small extra safety margin.
        for _ in range(20):
            try:
                conn = psycopg2.connect(
                    host=str(socket_dir), port=5432, dbname="postgres", user="postgres"
                )
                conn.close()
                break
            except psycopg2.OperationalError:
                time.sleep(0.25)
        else:
            raise RuntimeError(f"Postgres did not become ready; see {log_path}")

        yield {"socket_dir": str(socket_dir), "port": 5432, "user": "postgres"}
    finally:
        subprocess.run(
            ["pg_ctl", "-D", str(data_dir), "-m", "immediate", "stop"],
            capture_output=True,
        )
        shutil.rmtree(tmp_dir, ignore_errors=True)


@pytest.fixture()
def pg_dsn(pg_cluster):
    """Fresh database per test, migration applied, dropped afterward."""
    admin_conn = psycopg2.connect(
        host=pg_cluster["socket_dir"], port=pg_cluster["port"],
        dbname="postgres", user=pg_cluster["user"],
    )
    admin_conn.autocommit = True
    dbname = f"test_{int(time.time() * 1e6)}"
    with admin_conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE {dbname}")
    admin_conn.close()

    dsn = (
        f"host={pg_cluster['socket_dir']} port={pg_cluster['port']} "
        f"dbname={dbname} user={pg_cluster['user']}"
    )
    conn = psycopg2.connect(dsn)
    with open(MIGRATION_PATH) as f:
        migration_sql = f.read()
    with conn.cursor() as cur:
        cur.execute(migration_sql)
    conn.commit()
    conn.close()

    yield dsn

    admin_conn = psycopg2.connect(
        host=pg_cluster["socket_dir"], port=pg_cluster["port"],
        dbname="postgres", user=pg_cluster["user"],
    )
    admin_conn.autocommit = True
    with admin_conn.cursor() as cur:
        cur.execute(f"DROP DATABASE {dbname} WITH (FORCE)")
    admin_conn.close()
