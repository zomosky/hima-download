"""Discovery of downloaded Himawari files (no manifest exists, so we glob).

The downloader writes a flat, month-keyed tree with *no* sidecar/manifest:

    <data_dir>/<PRODUCT>/<YYYYMM>/<filename>.nc

All three products encode the UTC timestamp in the filename as ``_YYYYMMDD_HHMM_``
(PAR files start ``H09_...``; CLP/ARP start ``NC_H09_...``). PAR additionally ships two
grid widths in the same folder; we keep **only** the 5 km full-disk ``.02801_02401`` grid
(longitude 70-210, full-month coverage) and ignore the ``.02401_02401`` PAR variant
(longitude 80-200, present only for 2026-01-01..05) so a month has no duplicate timestamps.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

# Products handled by the restore step (matches hima_download.catalog.PRODUCTS keys).
PRODUCTS: tuple[str, ...] = ("PAR", "CLP", "ARP")

# _YYYYMMDD_HHMM_  -- matches both "H09_20260101_0000_..." and "NC_H09_20260101_0000_...".
_TS_RE = re.compile(r"_(\d{8})_(\d{4})_")
_MONTH_RE = re.compile(r"\d{6}")

# PAR keeps only the wide 5 km full-disk grid (longitude 70-210).
_PAR_KEEP_SUFFIX = ".02801_02401.nc"


def parse_time(name: str) -> datetime:
    """Extract the UTC timestamp encoded in a Himawari filename."""
    m = _TS_RE.search(name)
    if not m:
        raise ValueError(f"no _YYYYMMDD_HHMM_ timestamp in filename: {name!r}")
    dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M")
    return dt.replace(tzinfo=timezone.utc)


def is_kept(product: str, name: str) -> bool:
    """Whether a filename should be processed for ``product``.

    Only ``.nc`` files; for PAR only the wide ``.02801_02401`` full-disk grid.
    """
    if not name.endswith(".nc"):
        return False
    if product == "PAR":
        return name.endswith(_PAR_KEEP_SUFFIX)
    return True


def discover_months(data_dir: Path, product: str) -> list[str]:
    """List the ``YYYYMM`` subdirectories present on disk for ``product``."""
    base = data_dir / product
    if not base.is_dir():
        return []
    return sorted(
        p.name for p in base.iterdir() if p.is_dir() and _MONTH_RE.fullmatch(p.name)
    )


def list_files(
    data_dir: Path,
    product: str,
    month: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[Path]:
    """Sorted kept ``.nc`` files for one (product, month).

    ``start``/``end`` (UTC, end exclusive) optionally restrict to a sub-range of the
    month -- useful for quick tests or incremental partial runs.
    """
    d = data_dir / product / month
    if not d.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(d.glob("*.nc")):
        if not is_kept(product, p.name):
            continue
        if start is not None or end is not None:
            t = parse_time(p.name)
            if start is not None and t < start:
                continue
            if end is not None and t >= end:
                continue
        out.append(p)
    return out
