from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from pipeline.config import SeasonTarget, load_season_config, season_targets

FIXTURES = Path(__file__).parent / "fixtures"


def test_season_target_archive_key_regular():
    t = SeasonTarget(source="nbastats", year=2023, season_type="regular")
    assert t.archive_key == "nbastats_2023"
    assert t.table == "nba.pbp_event"


def test_season_target_archive_key_playoffs():
    t = SeasonTarget(source="shotdetail", year=2023, season_type="playoffs")
    assert t.archive_key == "shotdetail_po_2023"
    assert t.table == "nba.shot_detail"


def test_default_config_expands_to_all_four_sources_per_season(tmp_path):
    cfg = tmp_path / "seasons.yaml"
    cfg.write_text(
        yaml.dump({"seasons": [{"year": 2023, "season_types": ["regular", "playoffs"]}]})
    )
    targets = season_targets(cfg)
    keys = {t.archive_key for t in targets}
    assert keys == {
        "nbastats_2023", "nbastats_po_2023", "shotdetail_2023", "shotdetail_po_2023",
    }


def test_config_rejects_unknown_season_type(tmp_path):
    cfg = tmp_path / "seasons.yaml"
    cfg.write_text(yaml.dump({"seasons": [{"year": 2023, "season_types": ["preseason"]}]}))
    with pytest.raises(ValueError, match="Unknown season_type"):
        load_season_config(cfg)


def test_placeholder_seasons_yaml_loads():
    """The shipped pipeline/seasons.yaml is the real config, not a fixture -- confirm it
    parses and covers at least one season, as a config-driven placeholder should."""
    targets = season_targets()
    assert len(targets) > 0
    assert all(t.season_type in ("regular", "playoffs") for t in targets)
