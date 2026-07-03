"""``hima-restore`` CLI: crop downloaded Himawari NetCDF into per-(product, month) Zarr.

Subcommands mirror the climate_restorage patterns:
  run        process one (product, month) -- optionally a sub-range for quick tests
  scan-once  idempotent sweep over every (product, month) on disk (for cron)
  list-products
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from loguru import logger
from rich.console import Console
from rich.table import Table

from .catalog import PRODUCTS
from .config import RestoreConfig, load_config

app = typer.Typer(
    add_completion=False,
    help="Crop downloaded Himawari L2 NetCDF to a bbox and write per-(product, month) Zarr.",
)
console = Console()


def _parse_dt(s: str) -> datetime:
    """Parse 'YYYY-MM-DD[THH:MM]' or 'YYYYMMDD[HHMM]' as UTC."""
    for f in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d", "%Y%m%d%H%M", "%Y%m%d"):
        try:
            return datetime.strptime(s, f).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise typer.BadParameter(f"unrecognized datetime: {s!r}")


def _parse_bbox(s: str) -> tuple[float, float, float, float]:
    parts = [float(x.strip()) for x in s.split(",")]
    if len(parts) != 4:
        raise typer.BadParameter("bbox must be 'west,east,south,north'")
    return parts[0], parts[1], parts[2], parts[3]


def _parse_chunks(s: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for piece in s.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if "=" not in piece:
            raise typer.BadParameter(f"bad chunk spec {piece!r}, expected 'dim=int'")
        k, v = piece.split("=", 1)
        out[k.strip()] = int(v.strip())
    return out


def _load_cfg(
    config: Optional[Path],
    *,
    data_dir: Optional[str],
    output_dir: Optional[str],
    bbox: Optional[str],
    products: Optional[str],
    chunks: Optional[str],
    workers: Optional[int] = None,
) -> RestoreConfig:
    cfg = load_config(config) if config else RestoreConfig()
    if data_dir:
        cfg.data_dir = Path(data_dir).expanduser()
    if output_dir:
        cfg.output_dir = Path(output_dir).expanduser()
    if bbox:
        cfg.bbox = _parse_bbox(bbox)
    if products:
        cfg.products = [p.strip() for p in products.split(",") if p.strip()]
    if chunks:
        cfg.chunks = _parse_chunks(chunks)
    if workers is not None:
        cfg.workers = workers
    cfg.validate()
    return cfg


def _want_progress(no_progress: bool) -> bool:
    """Show the dask progress bar on an interactive terminal unless disabled."""
    return not no_progress and sys.stderr.isatty()


def _setup_logging(verbose: bool) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if verbose else "INFO",
        format="<green>{time:HH:mm:ss}</green> <level>{level: <7}</level> {message}",
    )


@app.callback()
def _root(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    _setup_logging(verbose)


@app.command()
def run(
    product: str = typer.Argument(..., help="PAR | CLP | ARP"),
    month: str = typer.Argument(..., help="YYYYMM, e.g. 202601"),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="restore job YAML"),
    data_dir: Optional[str] = typer.Option(None, help="override data_dir (download output root)"),
    output_dir: Optional[str] = typer.Option(None, help="override output_dir (.zarr root)"),
    bbox: Optional[str] = typer.Option(None, help="west,east,south,north (degrees)"),
    chunks: Optional[str] = typer.Option(None, help="zarr chunks 'time=-1,latitude=256,longitude=256'"),
    start: Optional[str] = typer.Option(None, help="UTC start (inclusive) to restrict the month"),
    end: Optional[str] = typer.Option(None, help="UTC end (exclusive) to restrict the month"),
    force: bool = typer.Option(False, "--force", help="reprocess even if the output is up to date"),
    workers: Optional[int] = typer.Option(None, help="dask worker threads (default: min(cpu, 4))"),
    no_progress: bool = typer.Option(False, "--no-progress", help="disable the progress bar"),
) -> None:
    """Convert one (product, month) to a cropped Zarr store."""
    from .convert import convert_month

    product = product.upper()
    if product not in PRODUCTS:
        raise typer.BadParameter(f"unknown product {product!r}; known: {list(PRODUCTS)}")
    cfg = _load_cfg(config, data_dir=data_dir, output_dir=output_dir, bbox=bbox, products=None,
                    chunks=chunks, workers=workers)
    status, out = convert_month(
        cfg.data_dir,
        product,
        month,
        output_dir=cfg.output_dir,
        bbox=cfg.bbox,
        chunks=cfg.chunks,
        compressor=cfg.compressor,
        clevel=cfg.clevel,
        consolidated=cfg.consolidated,
        progress=_want_progress(no_progress),
        workers=cfg.workers,
        start=_parse_dt(start) if start else None,
        end=_parse_dt(end) if end else None,
        force=force,
    )
    console.print(f"[bold]{status}[/]  {out}")


@app.command("scan-once")
def scan_once(
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="restore job YAML"),
    data_dir: Optional[str] = typer.Option(None, help="override data_dir"),
    output_dir: Optional[str] = typer.Option(None, help="override output_dir"),
    bbox: Optional[str] = typer.Option(None, help="west,east,south,north (degrees)"),
    products: Optional[str] = typer.Option(None, help="restrict to these products, comma-separated"),
    chunks: Optional[str] = typer.Option(None, help="zarr chunks override"),
    force: bool = typer.Option(False, "--force", help="reprocess even if outputs are up to date"),
    workers: Optional[int] = typer.Option(None, help="dask worker threads (default: min(cpu, 4))"),
    no_progress: bool = typer.Option(False, "--no-progress", help="disable the progress bar"),
) -> None:
    """Idempotently sweep every (product, month) on disk, then exit (for cron)."""
    from .convert import scan_all

    cfg = _load_cfg(config, data_dir=data_dir, output_dir=output_dir, bbox=bbox, products=products,
                    chunks=chunks, workers=workers)
    console.print(
        f"[bold]scan-once[/]  data_dir={cfg.data_dir}  output_dir={cfg.output_dir}  "
        f"products={cfg.products}  bbox={cfg.bbox}  workers={cfg.workers}"
    )
    tally = scan_all(
        cfg.data_dir,
        output_dir=cfg.output_dir,
        bbox=cfg.bbox,
        products=cfg.products,
        chunks=cfg.chunks,
        compressor=cfg.compressor,
        clevel=cfg.clevel,
        consolidated=cfg.consolidated,
        progress=_want_progress(no_progress),
        workers=cfg.workers,
        force=force,
    )
    t = Table(title="scan-once result", show_header=True)
    for k in ("processed", "skipped", "empty", "failed"):
        t.add_column(k, justify="right")
    t.add_row(*(str(tally[k]) for k in ("processed", "skipped", "empty", "failed")))
    console.print(t)
    if tally["failed"]:
        raise typer.Exit(code=1)


@app.command()
def rechunk(
    month: Optional[str] = typer.Option(None, help="YYYYMM to rechunk; default = current UTC month"),
    products: Optional[str] = typer.Option(None, help="restrict to these products, comma-separated"),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="restore job YAML"),
    output_dir: Optional[str] = typer.Option(None, help="override output_dir (.zarr root)"),
    chunks: Optional[str] = typer.Option(None, help="target chunks, e.g. 'time=-1,latitude=256,longitude=256'"),
    force: bool = typer.Option(False, "--force", help="rewrite even if time is already a single chunk"),
    workers: Optional[int] = typer.Option(None, help="dask worker threads (default: min(cpu, 4))"),
    no_progress: bool = typer.Option(False, "--no-progress", help="disable the progress bar"),
) -> None:
    """Defragment: rewrite each (product, month) Zarr with a single time chunk, reading ONLY
    the Zarr (never the NetCDF). Cheap fix for the fragmentation left by realtime appends --
    meant for a nightly cron. Defaults to the current UTC month, all configured products."""
    from .convert import rechunk_month

    cfg = _load_cfg(config, data_dir=None, output_dir=output_dir, bbox=None, products=products,
                    chunks=chunks, workers=workers)
    mon = month or datetime.now(timezone.utc).strftime("%Y%m")
    console.print(f"[bold]rechunk[/]  month={mon}  output_dir={cfg.output_dir}  "
                  f"products={cfg.products}  workers={cfg.workers}")
    tally = {"rechunked": 0, "skipped": 0, "missing": 0, "failed": 0}
    for product in cfg.products:
        try:
            status, out = rechunk_month(
                cfg.output_dir, product, mon,
                chunks=cfg.chunks, consolidated=cfg.consolidated,
                progress=_want_progress(no_progress), workers=cfg.workers, force=force,
            )
            tally[status] += 1
            console.print(f"  {product} {mon}: [cyan]{status}[/] -> {out}")
        except Exception as ex:  # noqa: BLE001 - keep going through the rest
            logger.error(f"rechunk failed {product} {mon}: {ex}")
            tally["failed"] += 1
    t = Table(title="rechunk result", show_header=True)
    for k in ("rechunked", "skipped", "missing", "failed"):
        t.add_column(k, justify="right")
    t.add_row(*(str(tally[k]) for k in ("rechunked", "skipped", "missing", "failed")))
    console.print(t)
    if tally["failed"]:
        raise typer.Exit(code=1)


@app.command("list-products")
def list_products() -> None:
    """List the products the restore step recognizes."""
    for p in PRODUCTS:
        console.print(p)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
