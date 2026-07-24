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
from dask.utils import SerializableLock
from loguru import logger

from .catalog import list_files, parse_time
from .crop import BBox, crop_bbox

# The PyPI netCDF4 wheel bundles a non-thread-safe HDF5; concurrent reads across dask
# worker threads corrupt the heap ("double free or corruption"). We serialize the HDF5
# I/O through one global lock while letting the CPU work (decode/crop/encode/compress) run
# in parallel -- so ``workers>1`` stays fast without crashing. SerializableLock also behaves
# correctly under a process scheduler (each process gets its own lock, and its own HDF5).
_HDF5_LOCK = SerializableLock()

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


# PAR ships per-frame scan start/end as float64 MJD (days since 1858-11-17) that open_zarr
# decodes to datetime64; on the regular grid the reindex leaves un-written slots as NaN -> NaT.
# CLP/ARP carry no such field, so we synthesize the same float64-MJD var from the frame's
# nominal time -- a per-slot "written" marker (real timestamp on a written slot, NaT on the
# month's pre-allocated empty slots) that downstream monitoring can read uniformly across products.
_MJD_EPOCH = np.datetime64("1858-11-17T00:00:00", "ns")
_MJD_UNITS = "days since 1858-11-17 0:0:0"


def _mjd_days(t: datetime) -> float:
    return float((_np_time(t) - _MJD_EPOCH) / np.timedelta64(1, "D"))


def _frames_per_day(minutes: list[str]) -> int:
    return len(minutes) * 24


def _expected_grid(period: str, minutes: list[str]) -> np.ndarray:
    """Full regular UTC time axis for a ``period`` (``YYYYMM`` month or ``YYYYMMDD`` day):
    every ``minutes`` slot of every hour of every day in the period.

    This fixed grid is the backbone of the regular-grid store: frames that were never
    downloaded (or not yet) occupy their slot as NaN, and any frame -- fresh or delayed --
    is written in place at its slot, so arrival order is irrelevant and the axis never grows.
    A day period yields one day's grid (``len(minutes) * 24`` slots); a month period the
    whole month.
    """
    import calendar

    y, m = int(period[:4]), int(period[4:6])
    days = [int(period[6:8])] if len(period) == 8 else range(1, calendar.monthrange(y, m)[1] + 1)
    mins = sorted(int(x) for x in minutes)
    out = [
        np.datetime64(datetime(y, m, d, h, mm), "ns")
        for d in days
        for h in range(24)
        for mm in mins
    ]
    return np.array(out, dtype="datetime64[ns]")


def _regular_chunks(chunks: dict[str, int], minutes: list[str]) -> dict[str, int]:
    """Chunk policy for a regular-grid store: time chunked by day (so a single-frame
    region-write only rewrites that day's chunk), spatial chunks from the user config."""
    return {**chunks, "time": _frames_per_day(minutes)}


def _mask_invalid(da: xr.DataArray) -> xr.DataArray:
    """Set out-of-``valid_range`` values to NaN (physical no-data marker).

    JAXA declares ``valid_min``/``valid_max`` (raw int units) but for some products the
    actual fill (e.g. CLP's -32766) differs from the declared ``missing_value`` (-32768),
    so xarray's mask-and-scale leaves the fill *unmasked* -- CLP's "no cloud / no retrieval"
    then decodes to a bogus ~-327.66 instead of NaN. Honoring ``valid_range`` masks those
    cleanly. No-op for vars without the attrs. Uses the source scale/offset (still in
    ``.encoding`` here) to convert the raw bounds to physical units.
    """
    vmin = da.attrs.get("valid_min")
    vmax = da.attrs.get("valid_max")
    if vmin is None or vmax is None:
        return da
    sc = float(da.encoding.get("scale_factor", 1.0))
    off = float(da.encoding.get("add_offset", 0.0))
    lo, hi = float(vmin) * sc + off, float(vmax) * sc + off
    return da.where((da >= min(lo, hi)) & (da <= max(lo, hi)))


def _preprocess(ds: xr.Dataset, *, bbox: BBox) -> xr.Dataset:
    """Per-file hook for ``open_mfdataset``: crop, keep 2-D fields, mask no-data, stamp time."""
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
    for v in list(ds.data_vars):  # out-of-valid-range -> NaN (re-encoded to the fill on write)
        ds[v] = _mask_invalid(ds[v])
    ds = ds.expand_dims(time=[_np_time(t)])
    # Per-slot written-marker: keep the source's real scan times (PAR), else synthesize from
    # the frame's nominal time (CLP/ARP). Stored as float64 MJD so open_zarr decodes to
    # datetime64 and reindex gaps become NaT -- identical representation across products.
    for name in ("start_time", "end_time"):
        if name not in ds.variables:
            ds[name] = xr.DataArray(
                np.array([_mjd_days(t)], dtype="float64"), dims="time", attrs={"units": _MJD_UNITS}
            )
    return ds


def _open_frames(files: list[Path], bbox: BBox) -> xr.Dataset:
    """Open + crop + concat a list of frames along ``time`` (lazy, sorted)."""
    ds = xr.open_mfdataset(
        files,
        engine="netcdf4",
        combine="nested",
        concat_dim="time",
        parallel=False,  # concurrent opens crash the non-thread-safe HDF5 wheel
        lock=_HDF5_LOCK,  # serialize HDF5 reads; decode/crop/encode still run in parallel
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
            if not keep:
                continue
            # Regular-grid gaps are NaN; an int-packed var needs a _FillValue to store them.
            dt = np.dtype(keep.get("dtype", s[v].dtype))
            if np.issubdtype(dt, np.integer) and "_FillValue" not in keep:
                info = np.iinfo(dt)
                keep["_FillValue"] = dt.type(keep.get("missing_value", info.min if dt.kind == "i" else info.max))
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


def out_path_for(output_dir: Path, product: str, period: str) -> Path:
    """Flat layout: ``<output_dir>/<product>/<period>_<product>.zarr`` where ``period`` is
    a ``YYYYMM`` month (legacy) or ``YYYYMMDD`` day (current)."""
    return (output_dir / product / f"{period}_{product}.zarr").resolve()


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


def _stored_source_count(out_path: Path) -> int:
    """The ``n_source_files`` stamped into a store at build time, or -1 if unreadable."""
    try:
        ds = xr.open_zarr(out_path, consolidated=True)
        try:
            return int(ds.attrs.get("n_source_files", -1))
        finally:
            ds.close()
    except Exception:
        return -1


def _is_complete(out_path: Path, n_files: int) -> bool:
    """A store is 'done' iff its stamped ``n_source_files`` equals the on-disk frame count.

    (On the regular grid the ``time`` axis is a fixed full-month grid, so the number of *real*
    frames is tracked in an attribute instead.) A month that gained frames rebuilds, and a
    crash mid-write (store unreadable -> -1) reprocesses.
    """
    return out_path.is_dir() and _stored_source_count(out_path) == n_files


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
    period: str,
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
    minutes: list[str] | None = None,
) -> tuple[str, Path]:
    """Full (re)build of one (product, ``period``) into a cropped Zarr store.

    ``period`` is a ``YYYYMMDD`` day (current) or ``YYYYMM`` month (legacy); the regular
    grid and store name follow the period granularity.

    Returns ``(status, out_path)`` where status is ``"processed"``, ``"skipped"`` (already
    complete), or ``"empty"`` (no input frames). A ``start``/``end`` sub-range bypasses the
    idempotency skip and always rebuilds (a partial-month run).

    Full-month builds emit a **regular grid**: the ``time`` axis is the complete
    :func:`_expected_grid` for the month, so never-downloaded / delayed frames occupy their
    slot as NaN and any later frame is written in place (see :func:`upsert_frames`). Time is
    chunked by day so a single-frame region-write only rewrites that day. ``start``/``end``
    sub-range runs stay compact (no reindex) since they're for quick tests.
    """
    minutes = minutes or ["00", "10"]
    month = period  # body uses ``month`` as the period label (day or month); grid/paths follow period
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
        if not partial_run:
            # Regular grid: union keeps every real frame and fills the missing slots with NaN.
            grid = np.union1d(_expected_grid(month, minutes), ds["time"].values.astype("datetime64[ns]"))
            n_gap = len(grid) - ds.sizes["time"]
            ds = ds.reindex(time=grid)
            eff_chunks = _regular_chunks(chunks, minutes)
        else:
            n_gap, eff_chunks = 0, chunks
        ds = ds.chunk(_resolve_chunks(ds, eff_chunks))
        _clean_encoding(ds)
        ds.attrs["n_source_files"] = len(files)
        ds.attrs["bbox"] = [float(x) for x in bbox]
        ds.attrs["product"] = product
        ds.attrs["period"] = period
        ds.attrs["month"] = period[:6]
        if not partial_run:
            ds.attrs["grid_minutes"] = ",".join(minutes)
            logger.info(f"[{product} {month}] regular grid: {ds.sizes['time']} slots ({n_gap} NaN gaps)")

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
    period: str,
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
    """Incrementally merge new frames into an existing (product, ``period``) Zarr along ``time``.

    Falls back to a full :func:`convert_month` when no (or a broken) store exists yet.
    Returns ``(status, out_path)`` where status is ``"appended"``, ``"skipped"`` (no new
    frames), ``"processed"`` (first full build), or ``"empty"``.

    Note: appending fragments the ``time`` chunking over many cycles; a periodic full
    rebuild (``hima-restore scan-once --force``, or letting a closed period rebuild) restores
    the clean single-chunk time layout.
    """
    month = period  # body uses ``month`` as the period label (day or month)
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


def _set_group_attr(out_path: Path, key: str, value, consolidated: bool) -> None:
    """Update one root-group attribute in place and re-consolidate metadata."""
    import zarr

    g = zarr.open_group(str(out_path), mode="a")
    g.attrs[key] = value
    if consolidated:
        zarr.consolidate_metadata(str(out_path))


def _contiguous_runs(positions: list[int]) -> list[tuple[int, list[int]]]:
    """Group (already-ascending) grid positions into runs of consecutive indices.

    Returns ``[(start_pos, [orig_index, ...]), ...]`` so each run is one region-write.
    """
    runs: list[tuple[int, list[int]]] = []
    for oi, p in sorted(enumerate(positions), key=lambda kp: kp[1]):
        if p < 0:
            continue
        if runs and p == runs[-1][0] + len(runs[-1][1]):
            runs[-1][1].append(oi)
        else:
            runs.append((p, [oi]))
    return runs


def upsert_frames(
    data_dir: Path,
    product: str,
    period: str,
    *,
    output_dir: Path,
    bbox: BBox,
    chunks: dict[str, int],
    minutes: list[str] | None = None,
    files: list[Path] | None = None,
    compressor: str = "zstd",
    clevel: int = 3,
    consolidated: bool = True,
    progress: bool = False,
    workers: int | None = None,
) -> tuple[str, Path]:
    """Write frames **in place** into their fixed slots of the regular-grid store.

    This replaces the append model for realtime: every frame -- freshly produced or a
    *delayed* one that only showed up in a later download window -- maps to a deterministic
    slot on :func:`_expected_grid` and is region-written there, so arrival order is
    irrelevant, no ``time`` axis growth happens, and there is no fragmentation to clean up.

    ``files`` restricts the write to specific frames (realtime passes just what it downloaded
    this cycle); when ``None`` all on-disk frames of the month are (re)written. Falls back to a
    full :func:`convert_month` when no (or a broken) store exists yet. Returns ``(status,
    out_path)`` with status ``"upserted"``, ``"skipped"``, ``"processed"`` (first build), or
    ``"empty"``.
    """
    minutes = minutes or ["00", "10"]
    month = period  # body uses ``month`` as the period label (day or month)
    out_path = out_path_for(output_dir, product, month)
    on_disk = list_files(data_dir, product, month)
    if not on_disk:
        logger.warning(f"[{product} {month}] no input frames; skipping")
        return "empty", out_path
    if not out_path.is_dir() or _stored_time_count(out_path) < 0:
        return convert_month(
            data_dir, product, month, output_dir=output_dir, bbox=bbox, chunks=chunks,
            compressor=compressor, clevel=clevel, consolidated=consolidated,
            progress=progress, workers=workers, force=True, minutes=minutes,
        )

    write_files = list(files) if files is not None else on_disk
    if not write_files:
        return "skipped", out_path

    store = xr.open_zarr(out_path, consolidated=consolidated)
    try:
        gidx = {t: i for i, t in enumerate(store["time"].values.astype("datetime64[s]"))}
    finally:
        store.close()

    # Spatial chunking must match the store; time is handled per region-write.
    spatial = {k: v for k, v in _regular_chunks(chunks, minutes).items() if k != "time"}
    written = 0
    with _compute_ctx(progress, workers):
        ds = _open_frames(write_files, bbox)
        ds = ds.chunk(_resolve_chunks(ds, spatial))
        positions = [int(gidx.get(t, -1)) for t in ds["time"].values.astype("datetime64[s]")]
        off_grid = sum(1 for p in positions if p < 0)
        if off_grid:
            logger.warning(f"[{product} {month}] {off_grid} frame(s) off the expected grid; skipped")
        ds_novars = ds.drop_vars(list(ds.coords))  # write only data vars into the region
        for start, idxs in _contiguous_runs(positions):
            # Materialize the (small) block: numpy-backed => one chunk, so a partial write
            # into a day-sized zarr chunk can't race. safe_chunks=False allows the partial
            # (read-modify-write) region write, which is safe here (sequential, one block).
            block = ds_novars.isel(time=idxs).load()
            block.to_zarr(out_path, mode="a", region={"time": slice(start, start + len(idxs))},
                          consolidated=consolidated, safe_chunks=False)
            written += len(idxs)
        ds.close()

    _set_group_attr(out_path, "n_source_files", len(on_disk), consolidated)
    logger.success(f"[{product} {month}] upserted {written} frame(s) in place -> {out_path}")
    return "upserted", out_path


def _time_chunk_count(out_path: Path, consolidated: bool) -> int:
    """Number of chunks along ``time`` in an existing store (>1 == fragmented), or -1."""
    try:
        ds = xr.open_zarr(out_path, consolidated=consolidated)
        try:
            return len(ds.chunksizes.get("time", ()))
        finally:
            ds.close()
    except Exception:
        return -1


def rechunk_month(
    output_dir: Path,
    product: str,
    month: str,
    *,
    chunks: dict[str, int],
    consolidated: bool = True,
    progress: bool = False,
    workers: int | None = None,
    force: bool = False,
) -> tuple[str, Path]:
    """Rewrite one (product, month) Zarr with the target (single-time-chunk) layout,
    reading **only the Zarr** -- never the NetCDF. This cheaply collapses the ``time``
    fragmentation left by many realtime ``append_month`` cycles.

    Reads the compact, already-cropped store and re-writes it; the int16 packing and
    compressor carried in each variable's ``.encoding`` are preserved (only the stale
    chunk-layout hints are dropped), so the result is byte-identical bar the chunking.
    The swap is done via a temp store + rename so a crash never leaves a half-written store
    in place.

    Returns ``(status, out_path)``: ``"rechunked"``, ``"skipped"`` (already <=1 time chunk,
    unless ``force``), or ``"missing"`` (no readable store).
    """
    out_path = out_path_for(output_dir, product, month)
    if not out_path.is_dir() or _stored_time_count(out_path) < 0:
        return "missing", out_path

    n_chunks = _time_chunk_count(out_path, consolidated)
    if not force and n_chunks <= 1:
        logger.info(f"[{product} {month}] time already in {max(n_chunks,1)} chunk(s); skipping")
        return "skipped", out_path

    tmp = out_path.parent / f".{out_path.stem}.rechunk.zarr"
    bak = out_path.parent / f".{out_path.stem}.bak.zarr"
    _remove(tmp)
    logger.info(f"[{product} {month}] rechunking ({n_chunks} time chunks) -> single chunk")
    with _compute_ctx(progress, workers):
        src = xr.open_zarr(out_path, consolidated=consolidated)
        src = src.chunk(_resolve_chunks(src, chunks))
        for v in src.variables:  # drop stale chunk hints only; keep int16 packing + compressor
            for k in ("chunks", "preferred_chunks"):
                src[v].encoding.pop(k, None)
        src.to_zarr(tmp, mode="w", consolidated=consolidated, zarr_format=2)
        src.close()
    # atomic-ish swap: out -> bak, tmp -> out, drop bak
    _remove(bak)
    out_path.rename(bak)
    tmp.rename(out_path)
    _remove(bak)
    logger.success(f"[{product} {month}] rechunked -> {out_path}")
    return "rechunked", out_path


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
    minutes: list[str] | None = None,
) -> dict[str, int]:
    """Idempotently full-build every (product, day) on disk. Returns a status tally."""
    from .catalog import discover_days

    tally = {"processed": 0, "skipped": 0, "empty": 0, "failed": 0}
    for product in products:
        for day in discover_days(data_dir, product):
            try:
                status, _ = convert_month(
                    data_dir,
                    product,
                    day,
                    output_dir=output_dir,
                    bbox=bbox,
                    chunks=chunks,
                    compressor=compressor,
                    clevel=clevel,
                    consolidated=consolidated,
                    progress=progress,
                    workers=workers,
                    force=force,
                    minutes=minutes,
                )
                tally[status] += 1
            except Exception as exc:  # noqa: BLE001 - keep sweeping other days
                logger.error(f"[{product} {day}] failed: {exc}")
                tally["failed"] += 1
    return tally
