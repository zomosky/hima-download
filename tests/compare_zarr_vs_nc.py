"""Ad-hoc check: does the restored Zarr match the NetCDF source at a given point/time?

For each product it:
  1. opens the monthly Zarr store,
  2. picks a few interior (lat, lon) points,
  3. samples time steps spread across the month,
  4. for each sampled time, opens the matching source .nc and reads the same point,
  5. compares the two values (NaN-aware, tight tolerance).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import xarray as xr

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hima_download.restore.catalog import list_files, parse_time  # noqa: E402
from hima_download.restore.crop import crop_bbox  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ZARR = ROOT / "zarr"
BBOX = (70.0, 140.0, 15.0, 55.0)

# product -> variable to compare
VARS = {"ARP": "AOT", "CLP": "CLOT", "PAR": "SWR"}
MONTH = "202601"
N_TIMES = 24   # sampled frames per product
N_PTS = 4      # sampled points per product


def pick_var(z: xr.Dataset, product: str) -> str:
    want = VARS.get(product)
    if want in z.data_vars:
        return want
    # fallback: first 2-D (lat,lon,time) float var
    for v in z.data_vars:
        if set(z[v].dims) >= {"time", "latitude", "longitude"}:
            return v
    return list(z.data_vars)[0]


def compare_product(product: str) -> bool:
    store = ZARR / product / f"{MONTH}_{product}.zarr"
    if not store.is_dir():
        print(f"[{product}] no zarr store, skip")
        return True
    z = xr.open_zarr(store)
    var = pick_var(z, product)
    files = list_files(DATA, product, MONTH)
    by_time = {np.datetime64(parse_time(f.name).replace(tzinfo=None), "s"): f for f in files}

    lats = z.latitude.values
    lons = z.longitude.values
    # interior points (avoid the very edges)
    lat_idx = np.linspace(len(lats) // 5, len(lats) * 4 // 5, N_PTS).astype(int)
    lon_idx = np.linspace(len(lons) // 5, len(lons) * 4 // 5, N_PTS).astype(int)
    pts = [(float(lats[i]), float(lons[j])) for i, j in zip(lat_idx, lon_idx)]

    times = z.time.values
    tsel = np.linspace(0, len(times) - 1, N_TIMES).astype(int)

    print(f"\n=== {product}  var={var}  store time={len(times)}  files={len(files)} ===")
    print(f"    points: {[(round(a,2), round(b,2)) for a,b in pts]}")

    max_abs = 0.0
    n_cmp = n_nan_ok = n_both_nan = mismatch = 0
    missing_file = 0

    for ti in tsel:
        t = times[ti]
        key = np.datetime64(t, "s")
        f = by_time.get(key)
        if f is None:
            missing_file += 1
            continue
        src = xr.open_dataset(f, decode_times=False)
        src = crop_bbox(src, BBOX)
        if var not in src.data_vars:
            src.close()
            continue
        for (lat0, lon0) in pts:
            zval = float(z[var].sel(time=t, latitude=lat0, longitude=lon0).values)
            sval = float(src[var].sel(latitude=lat0, longitude=lon0, method="nearest").values)
            n_cmp += 1
            if np.isnan(zval) and np.isnan(sval):
                n_both_nan += 1
                continue
            if np.isnan(zval) != np.isnan(sval):
                mismatch += 1
                print(f"    NaN mismatch @ t={t} ({lat0},{lon0}): zarr={zval} nc={sval}")
                continue
            d = abs(zval - sval)
            max_abs = max(max_abs, d)
            if d <= 1e-6 or d <= 1e-4 * max(abs(sval), 1.0):
                n_nan_ok += 1
            else:
                mismatch += 1
                print(f"    VALUE mismatch @ t={t} ({lat0},{lon0}): zarr={zval} nc={sval} d={d}")
        src.close()
    z.close()

    print(f"    compared={n_cmp}  equal={n_nan_ok}  both_NaN={n_both_nan}  "
          f"mismatch={mismatch}  missing_src={missing_file}  max|Δ|={max_abs:.3e}")
    return mismatch == 0


def main() -> int:
    ok = True
    for p in ("ARP", "CLP", "PAR"):
        ok &= compare_product(p)
    print("\n" + ("ALL MATCH ✅" if ok else "MISMATCHES FOUND ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
