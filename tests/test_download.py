"""Manifest-parsing tests only -- no network access. ensure_downloaded/download_archive
hit real HTTP and are exercised indirectly by pulling the real fixtures for this task,
not re-tested here with mocks that would just restate the implementation."""
from __future__ import annotations

from pipeline.download import parse_list_data


def test_parse_list_data_basic():
    text = (
        "nbastats_2023=https://github.com/shufinskiy/nba_data/raw/main/datasets/nbastats_2023.tar.xz\n"
        "shotdetail_2023=https://github.com/shufinskiy/nba_data/raw/main/datasets/shotdetail_2023.tar.xz\n"
    )
    manifest = parse_list_data(text)
    assert manifest["nbastats_2023"].endswith("nbastats_2023.tar.xz")
    assert manifest["shotdetail_2023"].endswith("shotdetail_2023.tar.xz")
    assert len(manifest) == 2


def test_parse_list_data_skips_blank_and_malformed_lines():
    text = "\n\nnbastats_2023=https://example.com/nbastats_2023.tar.xz\nnot_a_valid_line\n"
    manifest = parse_list_data(text)
    assert manifest == {"nbastats_2023": "https://example.com/nbastats_2023.tar.xz"}


def test_parse_list_data_empty_text():
    assert parse_list_data("") == {}
