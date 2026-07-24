"""Download-side catalog: file selection from an FTP hour-dir listing."""

from __future__ import annotations

from datetime import datetime, timezone

from hima_download.catalog import PRODUCTS, select_files


def test_select_files_par_keeps_only_wide_grid():
    """PAR: keep only the wide .02801_02401 full-disk grid; drop the .02401_02401
    variant (restore discards it anyway) and 1km Japan when not requested."""
    par = PRODUCTS["PAR"]
    t = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
    listing = [
        ("H09_20250101_0000_RFL021_FLDK.02801_02401.nc", 51_000_000),  # wide → keep
        ("H09_20250101_0000_RFL021_FLDK.02401_02401.nc", 40_000_000),  # narrow → drop
        ("H09_20250101_0000_rFL021_FLDK.02401_02001.nc", 20_000_000),  # 1km Japan → drop
    ]
    names = [n for n, _ in select_files(par, t, listing, par_include_japan=False)]
    assert names == ["H09_20250101_0000_RFL021_FLDK.02801_02401.nc"]


def test_select_files_par_include_japan_adds_1km():
    par = PRODUCTS["PAR"]
    t = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
    listing = [
        ("H09_20250101_0000_RFL021_FLDK.02801_02401.nc", 51_000_000),
        ("H09_20250101_0000_rFL021_FLDK.02401_02001.nc", 20_000_000),  # 1km Japan
    ]
    names = {n for n, _ in select_files(par, t, listing, par_include_japan=True)}
    assert names == {
        "H09_20250101_0000_RFL021_FLDK.02801_02401.nc",
        "H09_20250101_0000_rFL021_FLDK.02401_02001.nc",
    }


def test_select_files_non_par_returns_timestamp_match():
    clp = PRODUCTS["CLP"]
    t = datetime(2025, 1, 1, 0, 10, tzinfo=timezone.utc)
    listing = [
        ("NC_H09_20250101_0010_L2CLP010_FLDK.02401_02401.nc", 30_000_000),  # match
        ("NC_H09_20250101_0000_L2CLP010_FLDK.02401_02401.nc", 30_000_000),  # other slot
    ]
    names = [n for n, _ in select_files(clp, t, listing, par_include_japan=False)]
    assert names == ["NC_H09_20250101_0010_L2CLP010_FLDK.02401_02401.nc"]
