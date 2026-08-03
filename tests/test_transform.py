"""Parse/clean edge-case tests, written against real sample rows pulled from a live
nbastats_po_2023 / shotdetail_po_2023 archive (see tests/fixtures/) rather than invented
data. Covers the edge cases called out in .agents/p0_source_investigation.md:
missing/null fields, the nbastats duplicate-row case, SCOREMARGIN's literal "TIE",
GAME_DATE's YYYYMMDD integer form, GAME_ID's 8-digit form, and an empty archive.
"""
from __future__ import annotations

import datetime
from pathlib import Path

import pandas as pd
import pandas.api.types as ptypes
import pytest

from pipeline.transform import (
    PBP_EVENT_COLUMNS,
    SHOT_DETAIL_COLUMNS,
    clean_nbastats,
    clean_shotdetail,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> pd.DataFrame:
    return pd.read_csv(FIXTURES / name)


# --- nbastats -----------------------------------------------------------------


def test_clean_nbastats_produces_expected_columns():
    raw = _read("nbastats_po_2023_game_sample.csv")
    out = clean_nbastats(raw, season=2023, season_type="playoffs")
    assert list(out.columns) == PBP_EVENT_COLUMNS
    assert (out["season"] == 2023).all()
    assert (out["season_type"] == "playoffs").all()


def test_clean_nbastats_rejects_bad_season_type():
    raw = _read("nbastats_po_2023_game_sample.csv")
    with pytest.raises(ValueError):
        clean_nbastats(raw, season=2023, season_type="postseason")


def test_clean_nbastats_missing_column_raises():
    raw = _read("nbastats_po_2023_game_sample.csv").drop(columns=["SCOREMARGIN"])
    with pytest.raises(ValueError, match="missing expected column"):
        clean_nbastats(raw, season=2023, season_type="playoffs")


def test_clean_nbastats_null_fields_become_nullable():
    raw = _read("nbastats_po_2023_game_sample.csv")
    out = clean_nbastats(raw, season=2023, season_type="playoffs")
    # neutraldescription is null on almost every row in real data -- must survive as
    # missing, not crash and not get coerced to a sentinel like 0 or "nan".
    assert out["neutraldescription"].isna().any()
    assert out["player2_team_id"].isna().any()
    # player1_id/team_id: team-level events (rebounds, timeouts) hold a team id in
    # player1_id with player1_name/player1_team_id null -- must not crash on this shape.
    team_event_rows = out[out["player1_name"].isna()]
    assert len(team_event_rows) > 0
    assert team_event_rows["player1_id"].notna().all()


def test_clean_nbastats_scoremargin_tie_not_coerced_numeric():
    raw = _read("nbastats_po_2023_game_sample.csv")
    out = clean_nbastats(raw, season=2023, season_type="playoffs")
    tie_rows = out[out["scoremargin"] == "TIE"]
    assert len(tie_rows) > 0
    # dtype must stay text-like; a numeric coercion would turn "TIE" into NaN.
    assert not ptypes.is_numeric_dtype(out["scoremargin"])
    assert not tie_rows["scoremargin"].isna().any()
    assert (tie_rows["scoremargin"] == "TIE").all()


def test_clean_nbastats_game_id_is_bare_8_digit_int():
    raw = _read("nbastats_po_2023_game_sample.csv")
    out = clean_nbastats(raw, season=2023, season_type="playoffs")
    game_ids = out["game_id"].unique()
    assert len(game_ids) == 1
    gid = int(game_ids[0])
    assert gid == 42300102
    assert len(str(gid)) == 8
    # playoff games start with season-type digit 4.
    assert str(gid)[0] == "4"


def test_clean_nbastats_deduplicates_exact_duplicate_rows():
    raw = _read("nbastats_with_duplicate.csv")
    assert raw.duplicated(subset=["GAME_ID", "EVENTNUM"]).sum() == 1  # fixture sanity check

    out = clean_nbastats(raw, season=2023, season_type="playoffs")

    assert out.duplicated(subset=["game_id", "eventnum"]).sum() == 0
    assert len(out) == len(raw) - 1


def test_clean_nbastats_semantic_key_collision_raises():
    """Two different rows sharing (game_id, eventnum) is a real conflict, not the
    documented exact-duplicate case -- must raise loudly rather than silently drop data."""
    raw = _read("nbastats_po_2023_game_sample.csv").head(3).copy()
    raw.loc[1, "EVENTNUM"] = raw.loc[0, "EVENTNUM"]
    raw.loc[1, "HOMEDESCRIPTION"] = "a genuinely different event"

    with pytest.raises(ValueError, match="collision"):
        clean_nbastats(raw, season=2023, season_type="playoffs")


def test_clean_nbastats_empty_archive():
    raw = _read("nbastats_empty.csv")
    assert len(raw) == 0
    out = clean_nbastats(raw, season=2023, season_type="playoffs")
    assert list(out.columns) == PBP_EVENT_COLUMNS
    assert len(out) == 0


# --- shotdetail -----------------------------------------------------------------


def test_clean_shotdetail_produces_expected_columns():
    raw = _read("shotdetail_po_2023_game_sample.csv")
    out = clean_shotdetail(raw, season=2023, season_type="playoffs")
    assert list(out.columns) == SHOT_DETAIL_COLUMNS
    assert "grid_type" not in out.columns
    assert "shot_attempted_flag" not in out.columns


def test_clean_shotdetail_game_date_parses_yyyymmdd_to_real_date():
    raw = _read("shotdetail_po_2023_game_sample.csv")
    out = clean_shotdetail(raw, season=2023, season_type="playoffs")
    assert (raw["GAME_DATE"] == 20240424).all()  # fixture sanity check
    assert out["game_date"].nunique() == 1
    assert out["game_date"].iloc[0] == datetime.date(2024, 4, 24)


def test_clean_shotdetail_game_id_is_bare_8_digit_int():
    raw = _read("shotdetail_po_2023_game_sample.csv")
    out = clean_shotdetail(raw, season=2023, season_type="playoffs")
    gid = int(out["game_id"].unique()[0])
    assert gid == 42300102
    assert len(str(gid)) == 8


def test_clean_shotdetail_missing_column_raises():
    raw = _read("shotdetail_po_2023_game_sample.csv").drop(columns=["SHOT_ZONE_BASIC"])
    with pytest.raises(ValueError, match="missing expected column"):
        clean_shotdetail(raw, season=2023, season_type="playoffs")


def test_clean_shotdetail_empty_archive():
    raw = _read("shotdetail_empty.csv")
    assert len(raw) == 0
    out = clean_shotdetail(raw, season=2023, season_type="playoffs")
    assert list(out.columns) == SHOT_DETAIL_COLUMNS
    assert len(out) == 0
