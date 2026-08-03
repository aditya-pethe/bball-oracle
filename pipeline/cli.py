"""Entry point: `python -m pipeline.cli [--config PATH] [--cache-dir PATH] [--dsn DSN]`

Orchestrates download -> parse/clean -> load for every (source, year, season_type) unit
listed in the season config (pipeline/seasons.yaml by default).
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd

from pipeline import db, load
from pipeline.config import DEFAULT_SEASONS_PATH, season_targets
from pipeline.download import ensure_downloaded, fetch_list_data
from pipeline.transform import clean_nbastats, clean_shotdetail

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path(__file__).parent.parent / ".cache" / "nba_data"

CLEANERS = {
    "nbastats": clean_nbastats,
    "shotdetail": clean_shotdetail,
}


def run(
    config_path: Path = DEFAULT_SEASONS_PATH,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    dsn: str | None = None,
) -> None:
    cache_dir = Path(cache_dir)
    targets = season_targets(config_path)
    manifest = fetch_list_data()
    conn = db.connect(dsn)
    try:
        for target in targets:
            csv_path = ensure_downloaded(target, manifest, cache_dir)
            raw = pd.read_csv(csv_path)
            clean = CLEANERS[target.source](raw, target.year, target.season_type)
            inserted = load.load_season_partition(
                conn, target.table, clean, target.year, target.season_type
            )
            logger.info(
                "%s %s %s: loaded %d rows into %s",
                target.source, target.year, target.season_type, inserted, target.table,
            )
        for table in {t.table for t in targets}:
            load.vacuum_analyze(dsn or db.dsn_from_env(), table)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the nba_data ETL pipeline.")
    parser.add_argument("--config", type=Path, default=DEFAULT_SEASONS_PATH)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--dsn", default=None, help=f"Defaults to ${db.ENV_DSN_VAR}")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    run(config_path=args.config, cache_dir=args.cache_dir, dsn=args.dsn)


if __name__ == "__main__":
    main()
