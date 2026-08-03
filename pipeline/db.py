"""Postgres connection helpers. One idiomatic client (psycopg2) per AGENTS.md's stack."""
from __future__ import annotations

import os

import psycopg2

ENV_DSN_VAR = "NBA_PIPELINE_DATABASE_URL"


def dsn_from_env() -> str:
    dsn = os.environ.get(ENV_DSN_VAR)
    if not dsn:
        raise RuntimeError(
            f"{ENV_DSN_VAR} is not set. Point it at the target Postgres instance, e.g. "
            "postgresql://user:pass@host:port/dbname"
        )
    return dsn


def connect(dsn: str | None = None):
    return psycopg2.connect(dsn or dsn_from_env())


def apply_migration(conn, sql_path: str) -> None:
    with open(sql_path) as f:
        sql = f.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
