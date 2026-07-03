"""Verify + time the `rechunk` (defrag) path."""
from __future__ import annotations
import sys, time, shutil, subprocess
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import numpy as np, xarray as xr
from hima_download.restore.convert import rechunk_month, _time_chunk_count

CHUNKS = {"time": -1, "latitude": 256, "longitude": 256}
def nfiles(p): return sum(1 for _ in Path(p).rglob("*") if _.is_file())
def du(p): return subprocess.run(["du","-sh",str(p)],capture_output=True,text=True).stdout.split()[0]

# ---------- 1) 功能正确性:碎片 -> rechunk -> 校验 -> 二次跳过 ----------
tmp = Path("/tmp/rc_test"); shutil.rmtree(tmp, ignore_errors=True)
real = Path("zarr/ARP/202601_ARP.zarr")
la, lo = 35.0, 116.0
ref = xr.open_zarr(real)["AOT"].sel(latitude=la, longitude=lo, method="nearest").load()

# 造碎片(time 每块 20)写进 tmp/ARP/202601_ARP.zarr
frag = tmp / "ARP" / "202601_ARP.zarr"; frag.parent.mkdir(parents=True)
z = xr.open_zarr(real).chunk({"time":20,"latitude":256,"longitude":256})
for v in z.variables: z[v].encoding.pop("chunks",None)
z.to_zarr(frag, mode="w", consolidated=True, zarr_format=2); z.close()
print(f"碎片: time块={_time_chunk_count(frag,True)}  文件={nfiles(frag)}")

st, _ = rechunk_month(tmp, "ARP", "202601", chunks=CHUNKS, progress=False, workers=4)
after = xr.open_zarr(frag)["AOT"].sel(latitude=la, longitude=lo, method="nearest").load()
m = ~(np.isnan(ref.values)&np.isnan(after.values))
d = float(np.nanmax(np.abs(ref.values[m]-after.values[m])))
print(f"rechunk -> {st}  time块={_time_chunk_count(frag,True)}  文件={nfiles(frag)}  max|Δ|={d:.2e}")
st2, _ = rechunk_month(tmp, "ARP", "202601", chunks=CHUNKS, progress=False, workers=4)
print(f"二次调用 -> {st2}  (应为 skipped)")
shutil.rmtree(tmp, ignore_errors=True)

# ---------- 2) 每晚耗时估计:强制重排各产品当前 store(拷到 tmp,不动真数据)----------
print("\n每产品 rechunk 耗时(写主导,近似月末每晚成本):")
tot = 0.0
for product in ("ARP","CLP","PAR"):
    src = Path(f"zarr/{product}/202601_{product}.zarr")
    if not src.is_dir(): continue
    dst_root = Path(f"/tmp/rc_{product}"); shutil.rmtree(dst_root, ignore_errors=True)
    dst = dst_root / product / f"202601_{product}.zarr"; dst.parent.mkdir(parents=True)
    shutil.copytree(src, dst)
    zt = xr.open_zarr(dst); nt = zt.sizes["time"]; nv = len(zt.data_vars); zt.close()
    t = time.monotonic()
    rechunk_month(dst_root, product, "202601", chunks=CHUNKS, progress=False, workers=4, force=True)
    dt = time.monotonic()-t; tot += dt
    print(f"  {product}: {dt:5.1f}s   (time={nt}帧, {nv}变量, {du(src)})")
    shutil.rmtree(dst_root, ignore_errors=True)
print(f"\n三产品合计 ≈ {tot:.0f}s / 晚(整月满帧、写主导)")
