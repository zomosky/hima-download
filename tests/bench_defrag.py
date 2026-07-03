"""Benchmark: fix time-chunk fragmentation via (A) full rebuild from NetCDF vs
(B) rechunk the existing Zarr (no NetCDF re-read). Also measures point-timeseries read."""
from __future__ import annotations
import sys, time, shutil, subprocess
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import numpy as np, xarray as xr
from hima_download.restore.convert import convert_month

FRAG = Path("/tmp/arp_frag.zarr")          # fragmented (time chunk=2), built earlier
CLEAN = Path("zarr/ARP/202601_ARP.zarr")   # reference clean store
DEFRAG = Path("/tmp/arp_defrag.zarr")      # output of rechunk path
REBUILD = Path("/tmp/arp_rebuild.zarr")    # output of full NetCDF rebuild
BBOX = (70., 140., 15., 55.)
CHUNKS = {"time": -1, "latitude": 256, "longitude": 256}

def nfiles(p): return sum(1 for _ in Path(p).rglob("*") if _.is_file())
def du(p): return subprocess.run(["du","-sh",str(p)],capture_output=True,text=True).stdout.split()[0]

# --- read speed: point time series on fragmented store ---
la, lo = 35.0, 116.0
t=time.monotonic()
zf = xr.open_zarr(FRAG); s_frag = zf["AOT"].sel(latitude=la, longitude=lo, method="nearest").load(); zf.close()
print(f"[读时序·碎片版]  {time.monotonic()-t:.2f}s   (89064 文件)")

t=time.monotonic()
zc = xr.open_zarr(CLEAN); s_clean = zc["AOT"].sel(latitude=la, longitude=lo, method="nearest").load(); zc.close()
print(f"[读时序·整块版]  {time.monotonic()-t:.2f}s   (144 文件)")

# ================= 方案 B:直接重排已有 Zarr(不读 NetCDF) =================
if DEFRAG.exists(): shutil.rmtree(DEFRAG)
t=time.monotonic()
z = xr.open_zarr(FRAG)
z = z.chunk(CHUNKS)
for v in z.variables:                       # 清掉源分块编码,保留 int16 打包/压缩
    for k in ("chunks","preferred_chunks"):
        z[v].encoding.pop(k, None)
z.to_zarr(DEFRAG, mode="w", consolidated=True, zarr_format=2)
z.close()
dt_defrag = time.monotonic()-t
print(f"\n[方案B 重排Zarr]  {dt_defrag:.1f}s   → 文件数={nfiles(DEFRAG)}  体积={du(DEFRAG)}")

# ================= 方案 A:从 NetCDF 整月重建(workers=4) =================
if REBUILD.exists(): shutil.rmtree(REBUILD)
t=time.monotonic()
convert_month(Path("data"),"ARP","202601", output_dir=REBUILD, bbox=BBOX, chunks=CHUNKS,
              progress=False, workers=4, force=True)
dt_rebuild = time.monotonic()-t
print(f"[方案A 重建NC ]  {dt_rebuild:.1f}s   (读 1483 个 .nc)")

# --- 校验:两种方案结果与碎片版数值一致 ---
zb = xr.open_zarr(DEFRAG); s_b = zb["AOT"].sel(latitude=la, longitude=lo, method="nearest").load(); zb.close()
za = xr.open_zarr(REBUILD/"ARP"/"202601_ARP.zarr") if (REBUILD/"ARP").exists() else xr.open_zarr(REBUILD)
s_a = za["AOT"].sel(latitude=la, longitude=lo, method="nearest").load(); za.close()
def maxd(x,y):
    m=~(np.isnan(x)&np.isnan(y)); return float(np.nanmax(np.abs(x.values[m]-y.values[m]))) if m.any() else 0.0
print(f"\n数值校验 vs 碎片版:  方案B max|Δ|={maxd(s_b,s_frag):.2e}   方案A max|Δ|={maxd(s_a,s_frag):.2e}")
print(f"\n结论:重排Zarr 比 重建NC 快 {dt_rebuild/dt_defrag:.1f}×")
