"""read_example.py — 读取 hima-restore 产物 Zarr 的示例(也用作读取自测)。

用法:
    uv run python read_example.py zarr/ARP/202601_ARP.zarr

演示"区域时序"读取:选一个中国子区域 + 一个变量,一次 ``.load()`` 读入内存(区域小 → 只命中
少量 chunk),之后训练/分析走纯 numpy,无需再碰 GRIB/NetCDF。
"""

from __future__ import annotations

import sys

import numpy as np
import xarray as xr


def main(path: str) -> int:
    ds = xr.open_zarr(path)  # consolidated 默认,开启快
    print(f"store: {path}")
    print("dims:", dict(ds.sizes))
    print("vars:", list(ds.data_vars))
    t = ds.time.values
    print(f"time: {str(t[0])[:16]} -> {str(t[-1])[:16]}  ({ds.sizes['time']} 帧)")

    # 区域时序:取一个中国子区域(注意纬度降序 -> slice 高在前)
    region = ds.sel(latitude=slice(45.0, 35.0), longitude=slice(110.0, 120.0))
    print("region dims:", dict(region.sizes))

    var = next((v for v in ("AOT", "PAR", "SWR", "CLOT", "AE") if v in ds.data_vars), None)
    if var is None:
        print("no known variable to sample")
        ds.close()
        return 0
    arr = region[var].load()  # 一次 I/O 全部读入
    print(f"{var}: shape={tuple(arr.shape)} dtype={arr.dtype}")
    finite = np.isfinite(arr.values)
    print(f"{var} finite frac: {float(finite.mean()):.3f}")
    if finite.any():
        print(f"{var} range: {float(np.nanmin(arr.values)):.4f} -> {float(np.nanmax(arr.values)):.3f}")
    ds.close()
    return 0


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else "zarr"
    sys.exit(main(p))
