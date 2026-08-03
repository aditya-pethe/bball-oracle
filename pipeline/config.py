"""Season coverage config: which (source, year, season_type) units the pipeline loads.

Config lives in pipeline/seasons.yaml as a plain list of {year, season_types}. Expanding
coverage (a new season, or turning on playoffs for one already listed) is a config edit
plus a rerun, never a code change.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_SEASONS_PATH = Path(__file__).parent / "seasons.yaml"

SEASON_TYPES = ("regular", "playoffs")

# nba_data source key prefix -> target table.
SOURCE_TABLE = {
    "nbastats": "nba.pbp_event",
    "shotdetail": "nba.shot_detail",
}


@dataclass(frozen=True)
class SeasonTarget:
    """One (source, year, season_type): one archive to download, one reload partition."""

    source: str  # "nbastats" | "shotdetail"
    year: int  # 4-digit season-start year, e.g. 2023
    season_type: str  # "regular" | "playoffs"

    @property
    def archive_key(self) -> str:
        infix = "_po_" if self.season_type == "playoffs" else "_"
        return f"{self.source}{infix}{self.year}"

    @property
    def table(self) -> str:
        return SOURCE_TABLE[self.source]


def load_season_config(path: str | Path = DEFAULT_SEASONS_PATH) -> list[dict]:
    with open(path) as f:
        doc = yaml.safe_load(f) or {}
    seasons = doc.get("seasons", [])
    for entry in seasons:
        bad_types = set(entry.get("season_types", [])) - set(SEASON_TYPES)
        if bad_types:
            raise ValueError(
                f"Unknown season_type(s) {bad_types} for year {entry.get('year')}; "
                f"must be a subset of {SEASON_TYPES}"
            )
    return seasons


def season_targets(path: str | Path = DEFAULT_SEASONS_PATH) -> list[SeasonTarget]:
    """Expand the season config into concrete download/load units."""
    targets = []
    for entry in load_season_config(path):
        year = entry["year"]
        for season_type in entry.get("season_types", SEASON_TYPES):
            for source in SOURCE_TABLE:
                targets.append(SeasonTarget(source=source, year=year, season_type=season_type))
    return targets
