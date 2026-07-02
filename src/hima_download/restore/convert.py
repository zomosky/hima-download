"""Crop + month-concatenate downloaded Himawari NetCDF into one Zarr per (product, month).

Pipeline for one (product, month):

1. Glob the kept ``.nc`` frames (:mod:`.catalog`); PAR is restricted to the 02801 grid.
2. ``xr.open_mfdataset(..., preprocess=...)`` opens every frame lazily (dask). The
   preprocess hook crops to the bbox, drops auxiliary/navigation variables (keeping only
   2-D ``(latitude, longitude)`` science fields), and stamps a ``time`` coord parsed from
   the filename.
3. Concatenate along ``time``, sort, rechunk to the region-time-series chunk policy and
   write a compact Zarr store.

Two entry points:

* :func:`convert_month` -- full (re)build of the whole month (used by backfill / cron).
* :func:`append_month` -- incrementally merge only the frames not yet in an existing store
  along ``time`` (used by realtime, cheap per cycle).

Variables are decoded on read (default ``mask_and_scale=True``), so the packing info
(``scale_factor`` / ``add_offset`` / ``dtype`` / fill) lives in each variable's ``.encoding``
rather than its ``.attrs``. ``to_zarr`` then **re-packs to the original ``int16``** on write
(half the size of float32, downstream ``open_zarr`` decodes transparently). Keeping the
packing in ``.encoding`` -- not ``.attrs`` -- is what lets an incremental ``append`` re-encode
consistently against the existing store (mixing attrs+encoding for the same key raises).

Idempotency uses the number of ``time`` steps already in the store vs the number of frames
on disk: both the full-build and append paths keep that in sync automatically, so no extra
bookkeeping attribute is needed and a growing month is detected for free.
"""

from __future__ import annotations

import shutil
import sys
from contextlib import ExitStack
from datetime import datetime
from functools import partial
from pathlib import Path

import dask
import numpy as np
import xarray as xr
from dask.diagnostics import ProgressBar
from loguru import logger

from .catalog import list_files, parse_time
from .crop import BBox, crop_bbox

# Coordinates we always keep; everything else (band, geometry, scalar time/start/end) is
# auxiliary navigation metadata and is dropped before building the time axis.
_KEEP_COORDS = {"latitude", "longitude"}

# 2-D fields we drop even though they pass the (lat, lon) filter below.
# ``Hour`` is the per-pixel observation UT (0..24h). The source packs it as int16 with a
# *per-frame* ``add_offset`` (~the frame's whole hour) at scale 1e-4; a single month store
# can't re-encode that uniformly (int16 * 1e-4 spans only +/-3.27h -> non-zero-hour frames
# overflow). It is also redundant with the ``time`` coordinate for our use, so we skip it.
_DROP_VARS = {"Hour"}

# Serialization-layout encoding keys copied in from NetCDF that conflict with our explicit
# Zarr chunking/compressor. Cleared before writing; the CF packing keys (scale_factor,
# add_offset, dtype, _FillValue, missing_value) are deliberately kept so we re-pack to int16.
_STALE_ENC = (
    "chunks", "preferred_chunks", "chunksizes", "contiguous", "original_shape",
    "source", "filters", "compressor", "zlib", "shuffle", "complevel", "fletcher32",
    "endian", "szip", "blosc",
)


def _clean_encoding(ds: xr.Dataset) -> None:
    """Drop stale chunk/codec encoding hints (kept CF packing keys re-pack to int16)."""
    for v in ds.variables:
        enc = ds[v].encoding
        for key in _STALE_ENC:
            enc.pop(key, None)


def _compute_ctx(progress: bool, workers: int | None) -> ExitStack:
    """Context that optionally caps dask worker threads and shows a dask progress bar.

    Concurrency is dask's threaded scheduler (parallel file reads + chunk crop/encode);
    ``workers`` sets the thread count. The progress bar renders to stderr during the
    ``open_mfdataset`` + ``to_zarr`` compute.
    """
    stack = ExitStack()
    if workers and workers > 0:
        stack.enter_context(dask.config.set(num_workers=workers, scheduler="threads"))
    if progress:
        stack.enter_context(ProgressBar(out=sys.stderr))
    return stack


def _np_time(t: datetime) -> np.datetime64:
    return np.datetime64(t.replace(tzinfo=None), "ns")


def _frame_key(t: datetime) -> np.datetime64:
    """Second-precision key for deduping frames against an existing time axis."""
    return np.datetime64(t.replace(tzinfo=None), "s")


def _preprocess(ds: xr.Dataset, *, bbox: BBox) -> xr.Dataset:
    """Per-file hook for ``open_mfdataset``: crop, keep 2-D fields, stamp time."""
    src = ds.encoding.get("source", "")
    t = parse_time(Path(src).name)
    ds = crop_bbox(ds, bbox)
    # Collapse singleton dims (PAR ships a length-1 ``time`` dim) so per-band/geometry
    # auxiliary arrays can be filtered out cleanly.
    ds = ds.squeeze(drop=True)
    keep = [
        v for v in ds.data_vars
        if str(v) not in _DROP_VARS and set(map(str, ds[v].dims)) <= _KEEP_COORDS
    ]
    ds = ds[keep]
    drop = [c for c in ds.coords if c not in _KEEP_COORDS]
    if drop:
        ds = ds.drop_vars(drop, errors="ignore")
    return ds.expand_dims(time=[_np_time(t)])


def _open_frames(files: list[Path], bbox: BBox) -> xr.Dataset:
    """Open + crop + concat a list of frames along ``time`` (lazy, sorted)."""
    ds = xr.open_mfdataset(
        files,
        engine="netcdf4",
        combine="nested",
        concat_dim="time",
        parallel=True,
        decode_times=False,  # our time axis is built from filenames; skip the file's MJD time
        preprocess=partial(_preprocess, bbox=bbox),
        coords="minimal",
        compat="override",
        combine_attrs="drop_conflicts",
    )
    return ds.sortby("time")


def _resolve_chunks(ds: xr.Dataset, chunks: dict[str, int]) -> dict[str, int]:
    """Map a user dim->chunk request to concrete sizes (``<=0`` => whole dim)."""
    resolved: dict[str, int] = {}
    for dim, want in chunks.items():
        if dim not in ds.dims:
            continue
        size = int(ds.sizes[dim])
        resolved[dim] = size if (want is None or want <= 0) else min(int(want), size)
    return resolved


# CF packing keys to carry over from the source so the Zarr store re-packs to int16.
_PACK_KEYS = ("dtype", "scale_factor", "add_offset", "_FillValue", "missing_value")


def _packing_encoding(sample_file: Path, varnames: list[str]) -> dict[str, dict]:
    """Read the CF packing (dtype/scale/offset/fill) each variable had in the source.

    xarray drops ``.encoding`` across the preprocess transforms (squeeze/expand_dims/chunk),
    so we recover it from one representative frame and re-apply it at write time; otherwise
    the store would be written as native float and lose the compact int16 packing.
    """
    enc: dict[str, dict] = {}
    with xr.open_dataset(sample_file, decode_times=False) as s:
        for v in varnames:
            if v not in s.variables:
                continue
            src = s[v].encoding
            keep = {k: src[k] for k in _PACK_KEYS if k in src}
            if keep:
                enc[v] = keep
    return enc


def _build_encoding(
    sample_file: Path, varnames: list[str], compressor: str, clevel: int
) -> dict[str, dict]:
    """Per-variable Zarr encoding: original int16 packing + a Blosc compressor.

    Chunks come from the dask layout (set via ``.chunk``), so they are not encoded here.
    """
    enc = _packing_encoding(sample_file, varnames)
    comp = None
    if compressor != "none":
        from numcodecs import Blosc

        comp = Blosc(cname=compressor, clevel=clevel, shuffle=Blosc.SHUFFLE)
    out: dict[str, dict] = {}
    for v in varnames:
        spec = dict(enc.get(v, {}))
        if comp is not None:
            spec["compressor"] = comp
        if spec:
            out[v] = spec
    return out


def out_path_for(output_dir: Path, product: str, month: str) -> Path:
    """Flat layout: ``<output_dir>/<product>/<YYYYMM>_<product>.zarr``."""
    return (output_dir / product / f"{month}_{product}.zarr").resolve()


def _stored_time_count(out_path: Path) -> int:
    """Number of ``time`` steps in an existing store, or -1 if unreadable/missing."""
    try:
        ds = xr.open_zarr(out_path, consolidated=True)
        try:
            return int(ds.sizes.get("time", -1))
        finally:
            ds.close()
    except Exception:
        return -1


def _is_complete(out_path: Path, n_files: int) -> bool:
    """A store is 'done' iff it opens and holds exactly one ``time`` step per on-disk frame.

    So a month that has since gained frames (files > time steps) is rebuilt/appended, and a
    crash mid-write (store unreadable -> -1) is reprocessed.
    """
    return out_path.is_dir() and _stored_time_count(out_path) == n_files


def select_new_files(files: list[Path], existing_times) -> list[Path]:
    """Return the frames in ``files`` whose timestamp is not already in ``existing_times``.

    ``existing_times`` is an array/sequence of numpy ``datetime64`` (the store's time axis).
    Comparison is at second precision (frames are minute-aligned), which sidesteps ns/tz
    mismatches.
    """
    have = {np.datetime64(t, "s") for t in np.asarray(existing_times).astype("datetime64[s]")}
    return [f for f in files if _frame_key(parse_time(f.name)) not in have]


def _remove(out_path: Path) -> None:
    if not out_path.exists():
        return
    if out_path.is_dir():
        if out_path.suffix != ".zarr":
            raise RuntimeError(f"refusing to remove non-zarr directory: {out_path}")
        shutil.rmtree(out_path)
    else:
        out_path.unlink()


def convert_month(
    data_dir: Path,
    product: str,
    month: str,
    *,
    output_dir: Path,
    bbox: BBox,
    chunks: dict[str, int],
    compressor: str = "zstd",
    clevel: int = 3,
    consolidated: bool = True,
    progress: bool = False,
    workers: int | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    force: bool = False,
) -> tuple[str, Path]:
    """Full (re)build of one (product, month) into a cropped Zarr store.

    Returns ``(status, out_path)`` where status is ``"processed"``, ``"skipped"`` (already
    complete), or ``"empty"`` (no input frames). A ``start``/``end`` sub-range bypasses the
    idempotency skip and always rebuilds (a partial-month run).
    """
    partial_run = start is not None or end is not None
    files = list_files(data_dir, product, month, start=start, end=end)
    out_path = out_path_for(output_dir, product, month)
    if not files:
        logger.warning(f"[{product} {month}] no input frames; skipping")
        return "empty", out_path

    if not force and not partial_run and _is_complete(out_path, len(files)):
        logger.info(f"[{product} {month}] up to date ({len(files)} frames); skipping")
        return "skipped", out_path

    logger.info(f"[{product} {month}] opening {len(files)} frames (workers={workers or 'auto'})")
    _remove(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with _compute_ctx(progress, workers):
        ds = _open_frames(files, bbox)
        ds = ds.chunk(_resolve_chunks(ds, chunks))
        _clean_encoding(ds)
        ds.attrs["n_source_files"] = len(files)
        ds.attrs["bbox"] = [float(x) for x in bbox]
        ds.attrs["product"] = product
        ds.attrs["month"] = month

        enc = _build_encoding(files[0], list(map(str, ds.data_vars)), compressor, clevel)
        # Pin a fine-grained integer time encoding so later incremental appends stay faithful.
        # (Without this, a first build of only whole-hour frames makes xarray pick "hours since
        # ..." and appending :10 frames rounds/corrupts the appended timestamps.)
        enc["time"] = {
            "units": "seconds since 1970-01-01T00:00:00",
            "calendar": "proleptic_gregorian",
            "dtype": "int64",
        }
        logger.info(
            f"[{product} {month}] writing {out_path}  "
            f"vars={list(ds.data_vars)}  time={ds.sizes.get('time')}  "
            f"lat={ds.sizes.get('latitude')}  lon={ds.sizes.get('longitude')}"
        )
        ds.to_zarr(out_path, mode="w", consolidated=consolidated, zarr_format=2, encoding=enc)
    ds.close()
    logger.success(f"[{product} {month}] done -> {out_path}")
    return "processed", out_path


def append_month(
    data_dir: Path,
    product: str,
    month: str,
    *,
    output_dir: Path,
    bbox: BBox,
    chunks: dict[str, int],
    compressor: str = "zstd",
    clevel: int = 3,
    consolidated: bool = True,
    progress: bool = False,
    workers: int | None = None,
) -> tuple[str, Path]:
    """Incrementally merge new frames into an existing (product, month) Zarr along ``time``.

    Falls back to a full :func:`convert_month` when no (or a broken) store exists yet.
    Returns ``(status, out_path)`` where status is ``"appended"``, ``"skipped"`` (no new
    frames), ``"processed"`` (first full build), or ``"empty"``.

    Note: appending fragments the ``time`` chunking over many cycles; a periodic full
    rebuild (``hima-restore scan-once --force``, or letting a closed month rebuild) restores
    the clean single-chunk time layout.
    """
    files = list_files(data_dir, product, month)
    out_path = out_path_for(output_dir, product, month)
    if not files:
        logger.warning(f"[{product} {month}] no input frames; skipping")
        return "empty", out_path

    if not out_path.is_dir() or _stored_time_count(out_path) < 0:
        return convert_month(
            data_dir, product, month,
            output_dir=output_dir, bbox=bbox, chunks=chunks,
            compressor=compressor, clevel=clevel, consolidated=consolidated,
            progress=progress, workers=workers,
        )

    existing = xr.open_zarr(out_path, consolidated=consolidated)
    try:
        existing_times = np.asarray(existing["time"].values)
    finally:
        existing.close()

    new = select_new_files(files, existing_times)
    if not new:
        logger.info(f"[{product} {month}] no new frames; skipping")
        return "skipped", out_path

    # ``append_dim`` only extends the tail. If any new frame predates the store's latest
    # time (out-of-order / gap backfill), appending would leave ``time`` unsorted -- fall
    # back to a full rebuild so the axis stays monotonic.
    new_min = min(_np_time(parse_time(f.name)) for f in new)
    if existing_times.size and new_min <= np.asarray(existing_times).astype("datetime64[ns]").max():
        logger.info(f"[{product} {month}] {len(new)} out-of-order frame(s); full rebuild to keep time sorted")
        return convert_month(
            data_dir, product, month,
            output_dir=output_dir, bbox=bbox, chunks=chunks,
            compressor=compressor, clevel=clevel, consolidated=consolidated,
            progress=progress, workers=workers, force=True,
        )

    logger.info(f"[{product} {month}] appending {len(new)} new frame(s) along time")
    with _compute_ctx(progress, workers):
        ds_new = _open_frames(new, bbox)
        ds_new = ds_new.chunk(_resolve_chunks(ds_new, chunks))
        _clean_encoding(ds_new)
        ds_new.to_zarr(out_path, mode="a", append_dim="time", consolidated=consolidated)
    ds_new.close()
    logger.success(f"[{product} {month}] appended {len(new)} -> {out_path}")
    return "appended", out_path


def scan_all(
    data_dir: Path,
    *,
    output_dir: Path,
    bbox: BBox,
    products: list[str],
    chunks: dict[str, int],
    compressor: str = "zstd",
    clevel: int = 3,
    consolidated: bool = True,
    progress: bool = False,
    workers: int | None = None,
    force: bool = False,
) -> dict[str, int]:
    """Idempotently full-build every (product, month) on disk. Returns a status tally."""
    from .catalog import discover_months

    tally = {"processed": 0, "skipped": 0, "empty": 0, "failed": 0}
    for product in products:
        for month in discover_months(data_dir, product):
            try:
                status, _ = convert_month(
                    data_dir,
                    product,
                    month,
                    output_dir=output_dir,
                    bbox=bbox,
                    chunks=chunks,
                    compressor=compressor,
                    clevel=clevel,
                    consolidated=consolidated,
                    progress=progress,
                    workers=workers,
                    force=force,
                )
                tally[status] += 1
            except Exception as exc:  # noqa: BLE001 - keep sweeping other months
                logger.error(f"[{product} {month}] failed: {exc}")
                tally["failed"] += 1
    return tally
