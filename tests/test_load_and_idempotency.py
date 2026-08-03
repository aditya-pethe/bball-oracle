"""Integration tests against a disposable local Postgres cluster (tests/conftest.py),
schema applied from db/migrations/0001_create_nba_schema.sql. Never touches Supabase.

Covers: the load step producing the right row counts, the (season, season_type) scoped
reload (not a full-table truncate), and the idempotency requirement -- rerunning the
pipeline for an already-loaded season doesn't change row counts.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import psycopg2
import pytest

from pipeline.load import load_season_partition
from pipeline.transform import clean_nbastats, clean_shotdetail

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> pd.DataFrame:
    return pd.read_csv(FIXTURES / name)


def _count(conn, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        return cur.fetchone()[0]


def test_load_pbp_event_inserts_deduplicated_rows(pg_dsn):
    conn = psycopg2.connect(pg_dsn)
    raw = _read("nbastats_with_duplicate.csv")
    clean = clean_nbastats(raw, season=2023, season_type="playoffs")

    inserted = load_season_partition(conn, "nba.pbp_event", clean, 2023, "playoffs")

    assert inserted == len(clean)
    assert _count(conn, "nba.pbp_event") == len(raw) - 1  # the one exact duplicate collapses
    conn.close()


def test_load_shot_detail_round_trip(pg_dsn):
    conn = psycopg2.connect(pg_dsn)
    raw = _read("shotdetail_po_2023_game_sample.csv")
    clean = clean_shotdetail(raw, season=2023, season_type="playoffs")

    inserted = load_season_partition(conn, "nba.shot_detail", clean, 2023, "playoffs")

    assert inserted == len(raw)
    assert _count(conn, "nba.shot_detail") == len(raw)

    with conn.cursor() as cur:
        cur.execute("SELECT game_date FROM nba.shot_detail LIMIT 1")
        (game_date,) = cur.fetchone()
        assert game_date.isoformat() == "2024-04-24"
    conn.close()


def test_reload_is_scoped_to_season_partition_not_full_truncate(pg_dsn):
    conn = psycopg2.connect(pg_dsn)
    raw = _read("nbastats_po_2023_game_sample.csv")
    clean_2023 = clean_nbastats(raw, season=2023, season_type="playoffs")
    load_season_partition(conn, "nba.pbp_event", clean_2023, 2023, "playoffs")

    # A different season's rows: reuse the same fixture content but relabel as 2024 with a
    # different game_id so the two partitions don't collide on the primary key.
    other = raw.copy()
    other["GAME_ID"] = 42400999
    clean_2024 = clean_nbastats(other, season=2024, season_type="playoffs")
    load_season_partition(conn, "nba.pbp_event", clean_2024, 2024, "playoffs")

    assert _count(conn, "nba.pbp_event") == len(clean_2023) + len(clean_2024)

    # Reloading the 2023 partition must not touch 2024's rows.
    load_season_partition(conn, "nba.pbp_event", clean_2023, 2023, "playoffs")
    assert _count(conn, "nba.pbp_event") == len(clean_2023) + len(clean_2024)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM nba.pbp_event WHERE season = 2024")
        assert cur.fetchone()[0] == len(clean_2024)
    conn.close()


def test_idempotent_rerun_same_season_does_not_change_row_count(pg_dsn):
    """Deliverable #5: run the pipeline for one season already loaded, assert row counts
    don't change on rerun."""
    conn = psycopg2.connect(pg_dsn)
    raw_nbastats = _read("nbastats_po_2023_game_sample.csv")
    raw_shotdetail = _read("shotdetail_po_2023_game_sample.csv")

    clean_nb = clean_nbastats(raw_nbastats, season=2023, season_type="playoffs")
    clean_sd = clean_shotdetail(raw_shotdetail, season=2023, season_type="playoffs")

    load_season_partition(conn, "nba.pbp_event", clean_nb, 2023, "playoffs")
    load_season_partition(conn, "nba.shot_detail", clean_sd, 2023, "playoffs")

    count_pbp_before = _count(conn, "nba.pbp_event")
    count_shot_before = _count(conn, "nba.shot_detail")

    # Rerun the exact same load ("pipeline runs again for a season already loaded").
    load_season_partition(conn, "nba.pbp_event", clean_nb, 2023, "playoffs")
    load_season_partition(conn, "nba.shot_detail", clean_sd, 2023, "playoffs")

    assert _count(conn, "nba.pbp_event") == count_pbp_before
    assert _count(conn, "nba.shot_detail") == count_shot_before
    conn.close()


def test_load_rejects_raw_duplicates_via_primary_key_if_dedup_skipped(pg_dsn):
    """Sanity check that the PK is actually doing its job: COPYing un-deduplicated rows
    straight from source must fail loudly, confirming dedup isn't optional."""
    conn = psycopg2.connect(pg_dsn)
    raw = _read("nbastats_with_duplicate.csv")
    # Bypass clean_nbastats's dedup on purpose (but keep its int casting, otherwise this
    # would fail on a type mismatch rather than the duplicate-key violation under test).
    from pipeline.transform import NBASTATS_INT_COLUMNS, PBP_EVENT_COLUMNS

    undeduped = raw.rename(columns=str.lower)
    for col in [c.lower() for c in NBASTATS_INT_COLUMNS]:
        undeduped[col] = undeduped[col].astype("Int64")
    undeduped.insert(0, "season_type", "playoffs")
    undeduped.insert(0, "season", 2023)
    undeduped = undeduped[PBP_EVENT_COLUMNS]

    with pytest.raises(psycopg2.errors.UniqueViolation):
        load_season_partition(conn, "nba.pbp_event", undeduped, 2023, "playoffs")
    conn.rollback()
    conn.close()
