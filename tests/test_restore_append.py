"""Tests for incremental-append frame selection (dedup by timestamp)."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from hima_download.restore.convert import select_new_files


def _arp(hhmm: str) -> Path:
    return Path(f"NC_H09_20260101_{hhmm}_L2ARP031_FLDK.02401_02401.nc")


def test_select_new_files_dedups_by_timestamp():
    files = [_arp("0000"), _arp("0010"), _arp("0100"), _arp("0110")]
    existing = np.array(
        ["2026-01-01T00:00", "2026-01-01T00:10"], dtype="datetime64[ns]"
    )
    new = select_new_files(files, existing)
    assert [p.name for p in new] == [_arp("0100").name, _arp("0110").name]


def test_select_new_files_all_new_when_store_empty():
    files = [_arp("0000"), _arp("0010")]
    assert select_new_files(files, np.array([], dtype="datetime64[ns]")) == files


def test_select_new_files_none_new_when_all_present():
    files = [_arp("0000"), _arp("0010")]
    existing = np.array(["2026-01-01T00:00", "2026-01-01T00:10"], dtype="datetime64[ns]")
    assert select_new_files(files, existing) == []
