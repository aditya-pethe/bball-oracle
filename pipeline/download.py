"""Download step: fetch the nba_data key->url manifest, download only the archives the
season config actually asks for.

Per the source investigation, `list_data.txt` is the maintained key->URL manifest and is
more robust than hand-constructing archive URLs (it's the authoritative list of what
actually exists, including odd cases like split early seasons).
"""
from __future__ import annotations

import tarfile
from pathlib import Path

import requests

from pipeline.config import SeasonTarget

LIST_DATA_URL = "https://raw.githubusercontent.com/shufinskiy/nba_data/main/list_data.txt"


def fetch_list_data(url: str = LIST_DATA_URL, timeout: int = 30) -> dict[str, str]:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return parse_list_data(resp.text)


def parse_list_data(text: str) -> dict[str, str]:
    """Parse `key=url` lines into a dict. Blank lines and anything without `=` are skipped
    rather than raising, since the manifest isn't ours to validate strictly."""
    manifest: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, url = line.split("=", 1)
        manifest[key] = url
    return manifest


def download_archive(url: str, dest: Path, timeout: int = 60) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=timeout) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    return dest


def extract_csv(archive_path: Path, dest_dir: Path) -> Path:
    """Every nba_data archive is exactly one CSV named identically to the archive stem
    (confirmed in the source investigation) — no nested dirs, no sibling files."""
    with tarfile.open(archive_path, mode="r:xz") as tf:
        members = [m for m in tf.getmembers() if m.isfile()]
        if len(members) != 1:
            raise ValueError(f"Expected exactly one file in {archive_path}, found {len(members)}")
        member = members[0]
        target = (dest_dir / member.name).resolve()
        if not target.is_relative_to(dest_dir.resolve()):
            raise ValueError(f"Unsafe archive member path in {archive_path}: {member.name}")
        tf.extract(member, path=dest_dir)
        return target


def ensure_downloaded(target: SeasonTarget, manifest: dict[str, str], cache_dir: Path) -> Path:
    """Download + extract one archive if it isn't already cached; return the CSV path."""
    key = target.archive_key
    if key not in manifest:
        raise KeyError(f"'{key}' not found in list_data.txt manifest")

    csv_path = cache_dir / f"{key}.csv"
    if csv_path.exists():
        return csv_path

    archive_path = cache_dir / f"{key}.tar.xz"
    download_archive(manifest[key], archive_path)
    extracted = extract_csv(archive_path, cache_dir)
    if extracted != csv_path:
        extracted.rename(csv_path)
    archive_path.unlink(missing_ok=True)
    return csv_path
