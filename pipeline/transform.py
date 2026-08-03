"""Parse/clean step: turn a raw nba_data CSV (already read into a DataFrame) into rows
compatible with the `nba.pbp_event` / `nba.shot_detail` schema in
db/migrations/0001_create_nba_schema.sql.

Key requirements (see .agents/p0_source_investigation.md, .agents/p0_schema_design.md):
- `season`/`season_type` don't exist in source and are derived from the archive filename.
- `nbastats` has rare exact-duplicate rows in source that must be dropped before load, or
  the (game_id, eventnum) primary key rejects the load.
- `SCOREMARGIN` contains the literal string "TIE" and must never be coerced to numeric.
- `GAME_DATE` in `shotdetail` is a YYYYMMDD integer and must be parsed to a real date.
- `GAME_ID` is the source's bare 8-digit form -- no zero-padding/reconstruction needed.
"""
from __future__ import annotations

import pandas as pd

NBASTATS_SOURCE_COLUMNS = [
    "GAME_ID", "EVENTNUM", "EVENTMSGTYPE", "EVENTMSGACTIONTYPE", "PERIOD",
    "WCTIMESTRING", "PCTIMESTRING", "HOMEDESCRIPTION", "NEUTRALDESCRIPTION",
    "VISITORDESCRIPTION", "SCORE", "SCOREMARGIN", "PERSON1TYPE",
    "PLAYER1_ID", "PLAYER1_NAME", "PLAYER1_TEAM_ID", "PLAYER1_TEAM_CITY",
    "PLAYER1_TEAM_NICKNAME", "PLAYER1_TEAM_ABBREVIATION", "PERSON2TYPE",
    "PLAYER2_ID", "PLAYER2_NAME", "PLAYER2_TEAM_ID", "PLAYER2_TEAM_CITY",
    "PLAYER2_TEAM_NICKNAME", "PLAYER2_TEAM_ABBREVIATION", "PERSON3TYPE",
    "PLAYER3_ID", "PLAYER3_NAME", "PLAYER3_TEAM_ID", "PLAYER3_TEAM_CITY",
    "PLAYER3_TEAM_NICKNAME", "PLAYER3_TEAM_ABBREVIATION", "VIDEO_AVAILABLE_FLAG",
]

# Integer columns that are sometimes null in source (pandas reads them as float64).
# Cast to pandas' nullable Int64 so COPY sees "123" / "" rather than "123.0".
NBASTATS_INT_COLUMNS = [
    "GAME_ID", "EVENTNUM", "EVENTMSGTYPE", "EVENTMSGACTIONTYPE", "PERIOD",
    "PERSON1TYPE", "PLAYER1_ID", "PLAYER1_TEAM_ID",
    "PERSON2TYPE", "PLAYER2_ID", "PLAYER2_TEAM_ID",
    "PERSON3TYPE", "PLAYER3_ID", "PLAYER3_TEAM_ID",
    "VIDEO_AVAILABLE_FLAG",
]

PBP_EVENT_COLUMNS = ["season", "season_type"] + [c.lower() for c in NBASTATS_SOURCE_COLUMNS]

SHOTDETAIL_SOURCE_COLUMNS = [
    "GRID_TYPE", "GAME_ID", "GAME_EVENT_ID", "PLAYER_ID", "PLAYER_NAME", "TEAM_ID",
    "TEAM_NAME", "PERIOD", "MINUTES_REMAINING", "SECONDS_REMAINING", "EVENT_TYPE",
    "ACTION_TYPE", "SHOT_TYPE", "SHOT_ZONE_BASIC", "SHOT_ZONE_AREA", "SHOT_ZONE_RANGE",
    "SHOT_DISTANCE", "LOC_X", "LOC_Y", "SHOT_ATTEMPTED_FLAG", "SHOT_MADE_FLAG",
    "GAME_DATE", "HTM", "VTM",
]

# grid_type is a constant ("Shot Chart Detail"); shot_attempted_flag is always 1 -- both
# zero-information, dropped per the schema design (see COMMENT ON TABLE nba.shot_detail).
SHOTDETAIL_DROPPED_COLUMNS = ["GRID_TYPE", "SHOT_ATTEMPTED_FLAG"]

SHOTDETAIL_INT_COLUMNS = [
    "GAME_ID", "GAME_EVENT_ID", "PLAYER_ID", "TEAM_ID", "PERIOD",
    "MINUTES_REMAINING", "SECONDS_REMAINING", "SHOT_DISTANCE", "LOC_X", "LOC_Y",
    "SHOT_MADE_FLAG",
]

SHOT_DETAIL_COLUMNS = ["season", "season_type"] + [
    c.lower() for c in SHOTDETAIL_SOURCE_COLUMNS if c not in SHOTDETAIL_DROPPED_COLUMNS
]

SEASON_TYPES = ("regular", "playoffs")


def _require_columns(df: pd.DataFrame, expected: list[str], source_name: str) -> None:
    missing = [c for c in expected if c not in df.columns]
    if missing:
        raise ValueError(f"{source_name}: missing expected column(s) {missing}")


def _validate_season_type(season_type: str) -> None:
    if season_type not in SEASON_TYPES:
        raise ValueError(f"season_type must be one of {SEASON_TYPES}, got {season_type!r}")


def clean_nbastats(df: pd.DataFrame, season: int, season_type: str) -> pd.DataFrame:
    """Clean one nbastats CSV (already loaded into a DataFrame) into pbp_event rows.

    Drops exact-duplicate source rows (the documented nbastats data-quality issue), then
    raises if any (game_id, eventnum) pair still collides after dedup -- that would mean
    two genuinely different rows share a key, a real conflict the pipeline should surface
    rather than silently pass through to a confusing Postgres constraint-violation error.
    """
    _validate_season_type(season_type)
    _require_columns(df, NBASTATS_SOURCE_COLUMNS, "nbastats")

    df = df[NBASTATS_SOURCE_COLUMNS].drop_duplicates(keep="first").reset_index(drop=True)

    if len(df) > 0:
        dup_keys = df.duplicated(subset=["GAME_ID", "EVENTNUM"], keep=False)
        if dup_keys.any():
            offending = df.loc[dup_keys, ["GAME_ID", "EVENTNUM"]].drop_duplicates()
            raise ValueError(
                "nbastats: (GAME_ID, EVENTNUM) collision(s) survive whole-row dedup -- "
                "these are two different rows sharing a key, not exact duplicates: "
                f"{offending.to_dict('records')}"
            )

    out = df.rename(columns=str.lower)
    for col in [c.lower() for c in NBASTATS_INT_COLUMNS]:
        out[col] = out[col].astype("Int64")

    out.insert(0, "season_type", season_type)
    out.insert(0, "season", season)
    return out[PBP_EVENT_COLUMNS]


def clean_shotdetail(df: pd.DataFrame, season: int, season_type: str) -> pd.DataFrame:
    """Clean one shotdetail CSV into shot_detail rows: drop constant columns, parse
    GAME_DATE from its YYYYMMDD integer form into a real date."""
    _validate_season_type(season_type)
    _require_columns(df, SHOTDETAIL_SOURCE_COLUMNS, "shotdetail")

    df = df.drop(columns=SHOTDETAIL_DROPPED_COLUMNS).reset_index(drop=True)

    out = df.rename(columns=str.lower)
    for col in [c.lower() for c in SHOTDETAIL_INT_COLUMNS]:
        out[col] = out[col].astype("Int64")

    if len(out) > 0:
        out["game_date"] = pd.to_datetime(
            out["game_date"].astype("Int64").astype(str), format="%Y%m%d"
        ).dt.date
    else:
        out["game_date"] = pd.Series(dtype="object")

    out.insert(0, "season_type", season_type)
    out.insert(0, "season", season)
    return out[SHOT_DETAIL_COLUMNS]
