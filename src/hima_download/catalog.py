"""Product catalog: remote paths, file-name patterns, and target file selection.

All three L2 products share the directory layout:
    /pub/himawari/L2/<PROD>/<VER>/<YYYYMM>/<DD>/<hh>/

File-name conventions (per JAXA README_HimawariGeo_en.txt):
  PAR : H<NN>_YYYYMMDD_hhmm_RFL<VER>_FLDK.02801_02401.nc      (5km full-disk)
        H<NN>_YYYYMMDD_hhmm_rFL<VER>_FLDK.02701_02601.nc      (1km Japan)
  CLP : NC_H<NN>_YYYYMMDD_hhmm_L2CLP<VER>_FLDK.02401_02401.nc
  ARP : NC_H<NN>_YYYYMMDD_hhmm_L2ARP<VER>_FLDK.02401_02401.nc

We avoid hard-coding H08/H09 by matching the timestamp portion of names returned
from a LIST of the hour directory.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class Product:
    code: str           # PAR / CLP / ARP
    version: str        # e.g. "021"
    remote_root: str    # /pub/himawari/L2/<CODE>/<VER>


PRODUCTS: dict[str, Product] = {
    "PAR": Product("PAR", "021", "/pub/himawari/L2/PAR/021"),
    "CLP": Product("CLP", "010", "/pub/himawari/L2/CLP/010"),
    "ARP": Product("ARP", "031", "/pub/himawari/L2/ARP/031"),
}


def remote_hour_dir(product: Product, t: datetime) -> str:
    """Return the FTP directory holding 10-minute frames for that hour (UTC)."""
    return f"{product.remote_root}/{t:%Y%m}/{t:%d}/{t:%H}"


def expected_name_substr(product: Product, t: datetime) -> str:
    """Substring uniquely identifying a frame's file name within an hour dir.

    Examples for t=2026-06-01 00:10 UTC:
      PAR -> "_20260601_0010_RFL021_"   (5km full-disk; also "_rFL021_" for 1km)
      CLP -> "_20260601_0010_L2CLP010_"
      ARP -> "_20260601_0010_L2ARP031_"
    """
    ts = f"_{t:%Y%m%d}_{t:%H%M}_"
    if product.code == "PAR":
        return ts  # caller filters by RFL/rFL separately
    return f"{ts}L2{product.code}{product.version}_"


def is_par_japan(name: str) -> bool:
    """1km Japan file has lowercase 'rFL' marker."""
    return "_rFL" in name


def is_par_fulldisk(name: str) -> bool:
    """5km full-disk file has uppercase 'RFL' marker."""
    return "_RFL" in name


# Wide 5km full-disk grid (longitude 70-210). The narrower ``.02401_02401`` variant
# (longitude 80-200, only some dates) is dropped by restore anyway, so we don't download
# it — halves PAR bandwidth. Mirrors restore.catalog._PAR_KEEP_SUFFIX.
_PAR_FULLDISK_KEEP = ".02801_02401.nc"


def local_path(data_dir: Path, product: Product, t: datetime, filename: str) -> Path:
    """Flat local layout: <data_dir>/<PRODUCT>/<YYYYMM>/<filename>."""
    return data_dir / product.code / f"{t:%Y%m}" / filename


def select_files(
    product: Product,
    t: datetime,
    listing: list[tuple[str, int]],
    par_include_japan: bool,
) -> list[tuple[str, int]]:
    """Filter an hour-dir listing down to the file(s) matching timestamp t.

    listing: list of (filename, size_bytes) returned by FTP MLSD or parsed LIST.
    Returns the subset to download for this timeline.
    """
    substr = expected_name_substr(product, t)
    matched = [(n, s) for n, s in listing if substr in n and n.endswith(".nc")]
    if product.code != "PAR":
        return matched
    # Only the wide full-disk grid restore keeps; skip the discarded .02401_02401 variant.
    out = [(n, s) for n, s in matched if is_par_fulldisk(n) and n.endswith(_PAR_FULLDISK_KEEP)]
    if par_include_japan:
        out += [(n, s) for n, s in matched if is_par_japan(n)]
    return out
