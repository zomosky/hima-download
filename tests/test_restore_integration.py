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


def _jobs(files):
    """Build download Jobs (product ARP) whose local_path points at the given files."""
    arp = DL_PRODUCTS["ARP"]
    return [Job(arp, parse_time(f.name), "", f, 0) for f in files]


def _slot(ds, f):
    return ds["AOT"].sel(time=np.datetime64(parse_time(f.name).replace(tzinfo=None), "ns")).values


def _same(a, b):
    m = ~(np.isnan(a) & np.isnan(b))
    return bool((~m).all()) or float(np.nanmax(np.abs(a[m] - b[m]))) < 1e-3


@pytest.mark.skipif(len(_REAL_ARP) < 6, reason="need >=6 real ARP frames in data/ARP/202601")
def test_auto_restore_backfill_then_realtime(tmp_path):
    arp = sorted(_REAL_ARP, key=lambda f: parse_time(f.name))
    build, later = arp[:3], arp[3:5]

    data, out = tmp_path / "data", tmp_path / "zarr"
    dst = data / "ARP" / "202601"
    dst.mkdir(parents=True)
    cfg = RestoreConfig(
        data_dir=data, output_dir=out,
        chunks={"time": -1, "latitude": 256, "longitude": 256}, minutes=["00", "10"],
    )
    store = out / "ARP" / "202601_ARP.zarr"

    # reference: all 5 frames built at once onto the regular grid
    from hima_download.restore.convert import convert_month
    rdata = tmp_path / "refdata" / "ARP" / "202601"
    rdata.mkdir(parents=True)
    for f in build + later:
        shutil.copy(f, rdata / f.name)
    convert_month(tmp_path / "refdata", "ARP", "202601", output_dir=tmp_path / "refzarr",
                  bbox=cfg.bbox, chunks=cfg.chunks, minutes=cfg.minutes, force=True)
    ref = xr.open_zarr(tmp_path / "refzarr" / "ARP" / "202601_ARP.zarr")

    # --- backfill: first 3 frames -> full regular-grid build ---
    for f in build:
        shutil.copy(f, dst / f.name)
    _restore_months(_jobs(build), cfg, incremental=False)
    assert store.is_dir()
    ds = xr.open_zarr(store)
    grid_n = ds.sizes["time"]
    assert grid_n == ref.sizes["time"] > len(build)      # full month grid, not just 3 frames
    assert all(t in set(ds.time.values) for t in [np.datetime64(parse_time(f.name).replace(tzinfo=None), "ns") for f in build])
    assert all(_same(_slot(ds, f), _slot(ref, f)) for f in build)   # build slots correct
    # `later` slots not written yet -> NaN
    assert all(bool(np.isnan(_slot(ds, f)).all()) for f in later)
    ds.close()
    assert str(zarr.open(str(store / "AOT")).dtype) == "int16"      # re-packed, compact

    # --- realtime: later frames arrive -> in-place region-write (no axis growth) ---
    for f in later:
        shutil.copy(f, dst / f.name)
    _restore_months(_jobs(later), cfg, incremental=True)
    ds = xr.open_zarr(store)
    assert ds.sizes["time"] == grid_n                              # regular grid: no growth
    assert all(_same(_slot(ds, f), _slot(ref, f)) for f in build + later)  # everything matches ref
    ds.close()

    # --- realtime again with the same frames -> idempotent ---
    _restore_months(_jobs(later), cfg, incremental=True)
    ds = xr.open_zarr(store)
    assert ds.sizes["time"] == grid_n
    assert all(_same(_slot(ds, f), _slot(ref, f)) for f in build + later)
    ds.close()
    ref.close()
