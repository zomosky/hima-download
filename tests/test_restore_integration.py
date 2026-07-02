"""Integration test for the download-triggered auto-crop glue (no network).

Drives the CLI helpers ``_touched_months`` / ``_restore_months`` directly (the code wired
into ``backfill``/``realtime``) against real ARP frames if present, simulating "backfill
downloaded a month, then realtime added two more frames". Self-skips on a bare checkout with
no downloaded data, so the suite still passes offline.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

zarr = pytest.importorskip("zarr")
xr = pytest.importorskip("xarray")

from hima_download.catalog import PRODUCTS as DL_PRODUCTS
from hima_download.cli import _restore_months, _touched_months
from hima_download.planner import Job
from hima_download.restore.catalog import list_files, parse_time
from hima_download.restore.config import RestoreConfig

_REAL_DATA = Path(__file__).resolve().parents[1] / "data"
_REAL_ARP = list_files(_REAL_DATA, "ARP", "202601")


def test_touched_months_from_jobs():
    arp, clp = DL_PRODUCTS["ARP"], DL_PRODUCTS["CLP"]
    jobs = [
        Job(arp, datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc), "", Path(""), 0),
        Job(arp, datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc), "", Path(""), 0),
        Job(clp, datetime(2026, 2, 3, 6, 0, tzinfo=timezone.utc), "", Path(""), 0),
    ]
    assert _touched_months(jobs) == {("ARP", "202601"), ("CLP", "202602")}


def _fmt_files(files):
    return sorted(parse_time(f.name).strftime("%Y-%m-%dT%H:%M") for f in files)


def _fmt_store(times):
    return [str(np.datetime64(x, "m")) for x in times]


@pytest.mark.skipif(len(_REAL_ARP) < 4, reason="need >=4 real ARP frames in data/ARP/202601")
def test_auto_restore_backfill_then_realtime(tmp_path):
    arp = sorted(_REAL_ARP, key=lambda f: parse_time(f.name))
    # Build from whole-hour frames only -- that's what makes xarray pick a coarse "hours
    # since" time unit, so this guards the append-timestamp corruption regression.
    whole = [f for f in arp if parse_time(f.name).minute == 0]
    if len(whole) < 2:
        pytest.skip("need >=2 whole-hour ARP frames")
    build = whole[:2]
    build_max = parse_time(build[-1].name)
    later = [f for f in arp if parse_time(f.name) > build_max][:2]
    if len(later) < 2:
        pytest.skip("need >=2 later ARP frames for an in-order append")

    data, out = tmp_path / "data", tmp_path / "zarr"
    dst = data / "ARP" / "202601"
    dst.mkdir(parents=True)
    cfg = RestoreConfig(
        data_dir=data,
        output_dir=out,
        chunks={"time": -1, "latitude": 256, "longitude": 256},
    )
    store = out / "ARP" / "202601_ARP.zarr"

    # --- backfill: whole-hour frames -> full build ---
    for f in build:
        shutil.copy(f, dst / f.name)
    _restore_months({("ARP", "202601")}, cfg, incremental=False)
    assert store.is_dir()
    ds = xr.open_zarr(store)
    assert _fmt_store(ds.time.values) == _fmt_files(build)  # exact timestamps
    ds.close()
    assert str(zarr.open(str(store / "AOT")).dtype) == "int16"  # re-packed, compact

    # --- realtime: later frames arrive -> in-order incremental append ---
    for f in later:
        shutil.copy(f, dst / f.name)
    _restore_months({("ARP", "202601")}, cfg, incremental=True)
    ds = xr.open_zarr(store)
    got = _fmt_store(ds.time.values)
    ds.close()
    # faithful timestamps (no corruption), sorted, no duplicates
    assert got == _fmt_files(build + later)

    # --- realtime again with nothing new -> no-op ---
    _restore_months({("ARP", "202601")}, cfg, incremental=True)
    ds = xr.open_zarr(store)
    assert _fmt_store(ds.time.values) == _fmt_files(build + later)
    ds.close()
