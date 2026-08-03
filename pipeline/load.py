"""Load step: bulk load a cleaned DataFrame via Postgres COPY, scoped per
(season, season_type) so reload is a partition-level DELETE + COPY, never a full-table
truncate (project_plan.md §7, p0_datapipeline.md §4).
"""
from __future__ import annotations

import io

import pandas as pd


def _to_copy_buffer(df: pd.DataFrame) -> io.StringIO:
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False, na_rep="")
    buf.seek(0)
    return buf


def delete_season(conn, table: str, season: int, season_type: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM {table} WHERE season = %s AND season_type = %s",
            (season, season_type),
        )
        return cur.rowcount


def copy_dataframe(conn, table: str, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    columns = ", ".join(df.columns)
    buf = _to_copy_buffer(df)
    with conn.cursor() as cur:
        cur.copy_expert(
            f"COPY {table} ({columns}) FROM STDIN WITH (FORMAT csv, NULL '')", buf
        )
    return len(df)


def load_season_partition(conn, table: str, df: pd.DataFrame, season: int, season_type: str) -> int:
    """Delete the (season, season_type) partition and COPY in the cleaned rows, in one
    transaction so a failed load never leaves the partition half-deleted."""
    delete_season(conn, table, season, season_type)
    inserted = copy_dataframe(conn, table, df)
    conn.commit()
    return inserted


def vacuum_analyze(dsn: str, table: str) -> None:
    """Must run outside any transaction block; takes its own autocommit connection."""
    import psycopg2

    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"VACUUM ANALYZE {table}")
    finally:
        conn.close()
