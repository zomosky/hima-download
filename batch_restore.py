"""batch_restore.py — 批量把已下载的葵花历史数据裁剪成月度 Zarr(独立脚本)。

对 ``data/<PRODUCT>/<YYYYMM>/*.nc`` 里已下载的历史数据做一次性/可续跑的批量裁剪:
每个 (产品, 月) 拼成一个扁平 Zarr(裁到中国区、沿 time 维、int16 紧凑),幂等跳过已完整产物。

用法:
    uv run python batch_restore.py                       # 用下方 CONFIG 处理全部历史
    uv run python batch_restore.py --dry-run             # 只列出待处理 (产品,月),不执行
    uv run python batch_restore.py --force               # 重建已存在产物
    uv run python batch_restore.py --products ARP,CLP --month-start 202601 --month-end 202601

配置集中在顶部 "用户配置区";关键项也可用 CLI flag 覆盖(flag 优先)。它不联网,只读磁盘。
这与 CLI 的 ``hima-restore scan-once`` 等价,额外提供了按月区间过滤 + dry-run 清单 + 就地配置块。
"""

from __future__ import annotations

# ============================================================
# ====== 用户配置区(所有参数在此修改)========================
# ============================================================
DATA_DIR = "data"                       # 下载输出根:<DATA_DIR>/<PRODUCT>/<YYYYMM>/*.nc
OUTPUT_DIR = "zarr"                      # zarr 产物根:<OUTPUT_DIR>/<PRODUCT>/<YYYYMM>_<PRODUCT>.zarr
PRODUCTS = ["PAR", "CLP", "ARP"]        # 处理哪些产品(PAR 只用宽网格 02801)
MONTH_START: str | None = None          # "YYYYMM" 起(含);None = 不限
MONTH_END:   str | None = None          # "YYYYMM" 止(含);None = 不限
BBOX = (70.0, 140.0, 15.0, 55.0)        # west, east, south, north(中国区)
CHUNKS = {"time": -1, "latitude": 256, "longitude": 256}   # 偏向区域时序
COMPRESSOR = "zstd"                      # zstd | lz4 | blosclz | zlib | none
CLEVEL = 3
CONSOLIDATED = True
WORKERS: int | None = None               # dask 并发线程数;None=自动(全核)
SKIP_EXISTING = True                     # True=已完整则跳过(可续跑);False=总是重建
LOG_LEVEL = "INFO"
# ============================================================
# ====== 以下为执行逻辑,通常无需修改 =========================
# ============================================================

import argparse
import sys
from pathlib import Path

# 让脚本在未做 editable 安装时也能直接跑(优先用 uv run,其环境已装好本包)。
_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from loguru import logger

from hima_download.restore.catalog import discover_months, list_files
from hima_download.restore.convert import _is_complete, convert_month, out_path_for


def _in_range(month: str, lo: str | None, hi: str | None) -> bool:
    return (lo is None or month >= lo) and (hi is None or month <= hi)


def _discover(data_dir: Path, products: list[str], lo: str | None, hi: str | None):
    tasks: list[tuple[str, str]] = []
    for product in products:
        for month in discover_months(data_dir, product):
            if _in_range(month, lo, hi):
                tasks.append((product, month))
    return tasks


def main() -> int:
    ap = argparse.ArgumentParser(description="批量葵花裁剪 -> 月度 Zarr")
    ap.add_argument("--dry-run", action="store_true", help="只列出待处理,不执行")
    ap.add_argument("--force", action="store_true", help="即使产物已存在也重建")
    ap.add_argument("--data-dir", default=DATA_DIR)
    ap.add_argument("--output-dir", default=OUTPUT_DIR)
    ap.add_argument("--products", default=",".join(PRODUCTS))
    ap.add_argument("--month-start", default=MONTH_START)
    ap.add_argument("--month-end", default=MONTH_END)
    ap.add_argument("--workers", type=int, default=WORKERS, help="dask 并发线程数(默认自动)")
    ap.add_argument("--no-progress", action="store_true", help="关闭进度条")
    ap.add_argument("--log-level", default=LOG_LEVEL)
    args = ap.parse_args()

    logger.remove()
    logger.add(sys.stderr, level=args.log_level,
               format="<green>{time:HH:mm:ss}</green> <level>{level: <7}</level> {message}")

    data_dir = Path(args.data_dir).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    products = [p.strip() for p in args.products.split(",") if p.strip()]
    force = args.force or not SKIP_EXISTING

    tasks = _discover(data_dir, products, args.month_start, args.month_end)
    logger.info(f"discovered {len(tasks)} (product, month) under {data_dir}  "
                f"products={products}  months=[{args.month_start or '*'}, {args.month_end or '*'}]")

    if args.dry_run:
        for product, month in tasks:
            n = len(list_files(data_dir, product, month))
            done = _is_complete(out_path_for(output_dir, product, month), n)
            logger.info(f"  {product} {month}  frames={n}  {'DONE (skip)' if (done and not force) else 'TODO'}")
        return 0

    progress = (not args.no_progress) and sys.stderr.isatty()
    tally = {"processed": 0, "skipped": 0, "empty": 0, "failed": 0}
    for product, month in tasks:
        try:
            status, out = convert_month(
                data_dir, product, month,
                output_dir=output_dir, bbox=BBOX, chunks=CHUNKS,
                compressor=COMPRESSOR, clevel=CLEVEL, consolidated=CONSOLIDATED,
                progress=progress, workers=args.workers,
                force=force,
            )
            tally[status] += 1
        except Exception as exc:  # noqa: BLE001 - keep going through the rest
            logger.error(f"[{product} {month}] failed: {exc}")
            tally["failed"] += 1

    logger.info(f"batch done: processed={tally['processed']} skipped={tally['skipped']} "
                f"empty={tally['empty']} failed={tally['failed']}")
    return 1 if tally["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
