"""Tests for restore-side file discovery (no real data needed)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hima_download.restore.catalog import (
    discover_months,
    is_kept,
    list_files,
    parse_time,
)


def test_parse_time_par_and_nc_prefixes():
    assert parse_time("H09_20260101_0010_RFL021_FLDK.02801_02401.nc") == datetime(
        2026, 1, 1, 0, 10, tzinfo=timezone.utc
    )
    assert parse_time("NC_H09_20260101_1230_L2ARP031_FLDK.02401_02401.nc") == datetime(
        2026, 1, 1, 12, 30, tzinfo=timezone.utc
    )


def test_parse_time_rejects_bad_name():
    with pytest.raises(ValueError):
        parse_time("not_a_himawari_file.nc")


def test_is_kept_par_only_2801():
    assert is_kept("PAR", "H09_20260101_0000_RFL021_FLDK.02801_02401.nc") is True
    # the narrow 2401 PAR variant must be ignored to avoid duplicate timestamps
    assert is_kept("PAR", "H09_20260101_0000_RFL021_FLDK.02401_02401.nc") is False
    # CLP/ARP keep their single 2401 grid
    assert is_kept("ARP", "NC_H09_20260101_0000_L2ARP031_FLDK.02401_02401.nc") is True
    assert is_kept("CLP", "whatever.txt") is False


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")


def test_discover_and_list_files(tmp_path):
    par = tmp_path / "PAR" / "202601"
    for hhmm in ("0000", "0010", "0020"):
        _touch(par / f"H09_20260101_{hhmm}_RFL021_FLDK.02801_02401.nc")
    # a narrow PAR variant + a stray file that must be excluded
    _touch(par / "H09_20260101_0000_RFL021_FLDK.02401_02401.nc")
    _touch(par / ".DS_Store")
    _touch(tmp_path / "ARP" / "202602" / "NC_H09_20260201_0000_L2ARP031_FLDK.02401_02401.nc")

    assert discover_months(tmp_path, "PAR") == ["202601"]
    assert discover_months(tmp_path, "ARP") == ["202602"]
    assert discover_months(tmp_path, "CLP") == []

    files = list_files(tmp_path, "PAR", "202601")
    assert [f.name for f in files] == [
        "H09_20260101_0000_RFL021_FLDK.02801_02401.nc",
        "H09_20260101_0010_RFL021_FLDK.02801_02401.nc",
        "H09_20260101_0020_RFL021_FLDK.02801_02401.nc",
    ]


def test_list_files_time_range(tmp_path):
    par = tmp_path / "PAR" / "202601"
    for hhmm in ("0000", "0010", "0020", "0030"):
        _touch(par / f"H09_20260101_{hhmm}_RFL021_FLDK.02801_02401.nc")
    sub = list_files(
        tmp_path,
        "PAR",
        "202601",
        start=datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc),
        end=datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc),  # exclusive
    )
    assert [parse_time(f.name).strftime("%H%M") for f in sub] == ["0010", "0020"]
