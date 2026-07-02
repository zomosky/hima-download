"""Command-line interface."""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import typer
from loguru import logger
from rich.console import Console
from rich.table import Table

from .config import settings
from .downloader import run_jobs
from .ftp_client import FTPClient
from .planner import filter_missing, iter_timelines, plan_jobs
from .runconfig import load_run_config

app = typer.Typer(add_completion=False, help="Download Himawari L2 PV-related products from JAXA P-Tree FTP.")
console = Console()


def _parse_dt(s: str) -> datetime:
    """Parse 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' or 'YYYYMMDDHHMM' as UTC."""
    fmts = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d", "%Y%m%d%H%M", "%Y%m%d")
    for f in fmts:
        try:
            return datetime.strptime(s, f).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise typer.BadParameter(f"Unrecognized datetime: {s!r}")


_FILE_SINK_ID: int | None = None


def _setup_logging(verbose: bool) -> None:
    global _FILE_SINK_ID
    logger.remove()
    logger.add(sys.stderr, level="DEBUG" if verbose else "INFO",
               format="<green>{time:HH:mm:ss}</green> <level>{level: <7}</level> {message}")
    # Persist logs to disk (lazy mkdir; rotate by size; retain by age).
    try:
        settings.log_file.parent.mkdir(parents=True, exist_ok=True)
        _FILE_SINK_ID = logger.add(
            str(settings.log_file),
            level="DEBUG" if verbose else "INFO",
            rotation=settings.log_rotation,
            retention=settings.log_retention,
            enqueue=True,  # safe across threads
            encoding="utf-8",
            format="{time:YYYY-MM-DD HH:mm:ss} {level: <7} {name}:{line} | {message}",
        )
    except Exception as e:
        logger.warning(f"Could not open log file {settings.log_file}: {e}")


def _reattach_file_log(verbose: bool) -> None:
    """Re-attach the file sink after settings overrides (e.g. YAML load)."""
    global _FILE_SINK_ID
    if _FILE_SINK_ID is not None:
        try:
            logger.remove(_FILE_SINK_ID)
        except ValueError:
            pass
        _FILE_SINK_ID = None
    try:
        settings.log_file.parent.mkdir(parents=True, exist_ok=True)
        _FILE_SINK_ID = logger.add(
            str(settings.log_file),
            level="DEBUG" if verbose else "INFO",
            rotation=settings.log_rotation,
            retention=settings.log_retention,
            enqueue=True,
            encoding="utf-8",
            format="{time:YYYY-MM-DD HH:mm:ss} {level: <7} {name}:{line} | {message}",
        )
    except Exception as e:
        logger.warning(f"Could not open log file {settings.log_file}: {e}")


def _print_settings() -> None:
    t = Table(title="hima-download settings", show_header=False)
    t.add_row("FTP host", settings.ftp_host)
    t.add_row("data_dir", str(settings.data_dir))
    t.add_row("products", ",".join(settings.products))
    t.add_row("minutes", ",".join(settings.minutes))
    t.add_row("concurrency", str(settings.concurrency))
    t.add_row("PAR include Japan (1km)", str(settings.par_include_japan))
    t.add_row("realtime window/interval", f"{settings.realtime_window_hours}h / {settings.realtime_interval_sec}s")
    t.add_row("log file", str(settings.log_file))
    t.add_row("log rotation/retention", f"{settings.log_rotation} / {settings.log_retention}")
    console.print(t)


_VERBOSE = False


@app.callback()
def _root(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    global _VERBOSE
    _VERBOSE = verbose
    _setup_logging(verbose)
    settings.validate()


@app.command()
def info() -> None:
    """Show current configuration."""
    _print_settings()


@app.command()
def probe() -> None:
    """Quick FTP health-check: login + list root + list latest hour for each product."""
    with FTPClient() as c:
        root = c.listdir("/")
        console.print(f"[green]Login OK[/]. Root entries: {len(root)}")
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        for pcode in settings.products:
            from .catalog import PRODUCTS, remote_hour_dir
            for back in range(0, 6):
                t = now - timedelta(hours=back)
                d = remote_hour_dir(PRODUCTS[pcode], t)
                items = c.listdir(d)
                if items:
                    console.print(f"  {pcode}  {d}  [cyan]{len(items)} files[/], latest delay ≈ {back}h")
                    break
            else:
                console.print(f"  {pcode}  [yellow]no data in last 6h[/]")


def _build_restore_cfg(restore_config: Optional[Path]):
    """Build a RestoreConfig for the auto-crop step; its data_dir is pinned to the
    download output so restore reads exactly what was just written."""
    from .restore.config import RestoreConfig, load_config

    cfg = load_config(restore_config) if restore_config else RestoreConfig()
    cfg.data_dir = settings.data_dir
    cfg.validate()
    return cfg


def _touched_months(jobs) -> set[tuple[str, str]]:
    """Unique (product_code, YYYYMM) pairs covered by a list of jobs."""
    return {(j.product.code, j.timeline.strftime("%Y%m")) for j in jobs}


def _restore_months(months: set[tuple[str, str]], restore_cfg, *, incremental: bool) -> None:
    """Crop the given (product, month) pairs to Zarr. ``incremental`` appends only new
    frames (realtime); otherwise the whole month is (re)built (backfill)."""
    from .restore.convert import append_month, convert_month

    fn = append_month if incremental else convert_month
    progress = sys.stderr.isatty()
    for product, month in sorted(months):
        try:
            status, out = fn(
                restore_cfg.data_dir, product, month,
                output_dir=restore_cfg.output_dir, bbox=restore_cfg.bbox,
                chunks=restore_cfg.chunks, compressor=restore_cfg.compressor,
                clevel=restore_cfg.clevel, consolidated=restore_cfg.consolidated,
                progress=progress, workers=restore_cfg.workers,
            )
            console.print(f"  [green]restore[/] {product} {month}: {status} → {out}")
        except Exception as ex:  # noqa: BLE001 - one month's failure shouldn't abort the rest
            logger.error(f"restore failed {product} {month}: {ex}")


def _do_backfill(
    s: datetime, e: datetime, prods: list[str], dry_run: bool, restore_cfg=None
) -> None:
    if e <= s:
        raise typer.BadParameter("end must be after start")
    timelines = iter_timelines(s, e, settings.minutes)
    console.print(f"[bold]Backfill[/] {s} → {e}  products={prods}  minutes={settings.minutes}  "
                  f"timelines={len(timelines)}")
    jobs = plan_jobs(None, prods, timelines)
    todo, have = filter_missing(jobs)
    console.print(f"  planned: {len(jobs)}  already on disk: {len(have)}  to download: {len(todo)}  "
                  f"~{sum(j.expected_size for j in todo)/1e9:.2f} GB")
    if not dry_run and todo:
        run_jobs(todo)
    if restore_cfg is not None and not dry_run:
        _restore_months(_touched_months(jobs), restore_cfg, incremental=False)


@app.command()
def backfill(
    start: str = typer.Argument(..., help="UTC start (inclusive), e.g. 2025-01-01 or 2025-01-01T00:00"),
    end: str = typer.Argument(..., help="UTC end (exclusive), e.g. 2025-01-02"),
    products: Optional[str] = typer.Option(None, help="Override HIMA_PRODUCTS, comma-separated"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Only plan, do not download"),
    restore: bool = typer.Option(False, "--restore", help="After download, crop the touched months to Zarr (full rebuild)"),
    restore_config: Optional[Path] = typer.Option(None, "--restore-config", help="restore job YAML (bbox/output_dir/chunks)"),
) -> None:
    """Download all selected products between [start, end) UTC."""
    prods = [p.strip() for p in (products.split(",") if products else settings.products) if p.strip()]
    rcfg = _build_restore_cfg(restore_config) if restore else None
    _do_backfill(_parse_dt(start), _parse_dt(end), prods, dry_run, rcfg)


@app.command()
def verify(
    start: str = typer.Argument(..., help="UTC start (inclusive)"),
    end: str = typer.Argument(..., help="UTC end (exclusive)"),
    products: Optional[str] = typer.Option(None, help="Override HIMA_PRODUCTS"),
) -> None:
    """Report which timelines / files are missing on disk for [start, end)."""
    s = _parse_dt(start)
    e = _parse_dt(end)
    prods = [p.strip() for p in (products.split(",") if products else settings.products) if p.strip()]
    timelines = iter_timelines(s, e, settings.minutes)
    jobs = plan_jobs(None, prods, timelines)
    todo, have = filter_missing(jobs)
    t = Table(title=f"Verify {s} → {e}", show_header=True)
    t.add_column("product"); t.add_column("on-disk", justify="right"); t.add_column("missing", justify="right")
    for p in prods:
        oh = sum(1 for j in have if j.product.code == p)
        ms = sum(1 for j in todo if j.product.code == p)
        t.add_row(p, str(oh), str(ms))
    console.print(t)
    if todo:
        console.print(f"[yellow]Missing {len(todo)} files (~{sum(j.expected_size for j in todo)/1e9:.2f} GB). "
                      f"Run `backfill {start} {end}` to fetch.[/]")
        for j in todo[:20]:
            console.print(f"  [red]MISS[/] {j.product.code}  {j.timeline:%Y-%m-%d %H:%M}  {j.remote_path.split('/')[-1]}")
        if len(todo) > 20:
            console.print(f"  ... and {len(todo)-20} more")
    else:
        console.print("[green]All files present.[/]")


def _do_realtime(win: int, interval: int, once: bool, restore_cfg=None) -> None:
    console.print(f"[bold]Realtime[/] window={win}h interval={interval}s products={settings.products} "
                  f"minutes={settings.minutes}  (Ctrl-C to stop)")
    while True:
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        end = now + timedelta(minutes=1)
        start = (now - timedelta(hours=win)).replace(minute=0)
        timelines = iter_timelines(start, end, settings.minutes)
        try:
            jobs = plan_jobs(None, settings.products, timelines)
        except Exception as ex:
            logger.error(f"Plan cycle failed: {ex}")
            jobs = []
        todo, have = filter_missing(jobs)
        if todo:
            console.print(f"[cyan]{now:%Y-%m-%d %H:%M UTC}[/]  new: {len(todo)}  on-disk: {len(have)}  "
                          f"~{sum(j.expected_size for j in todo)/1e9:.2f} GB")
            run_jobs(todo)
            if restore_cfg is not None:
                _restore_months(_touched_months(todo), restore_cfg, incremental=True)
        else:
            console.print(f"[dim]{now:%Y-%m-%d %H:%M UTC}  nothing new ({len(have)} on disk in window)[/]")
        if once:
            return
        time.sleep(interval)


@app.command()
def realtime(
    window_hours: int = typer.Option(None, help="Override HIMA_REALTIME_WINDOW_HOURS"),
    interval_sec: int = typer.Option(None, help="Override HIMA_REALTIME_INTERVAL_SEC"),
    once: bool = typer.Option(False, "--once", help="Run a single cycle and exit"),
    restore: bool = typer.Option(False, "--restore", help="After each cycle, append new frames of touched months to Zarr"),
    restore_config: Optional[Path] = typer.Option(None, "--restore-config", help="restore job YAML (bbox/output_dir/chunks)"),
) -> None:
    """Poll the FTP for the newest frames every interval_sec; download what's missing."""
    rcfg = _build_restore_cfg(restore_config) if restore else None
    _do_realtime(
        window_hours or settings.realtime_window_hours,
        interval_sec or settings.realtime_interval_sec,
        once,
        rcfg,
    )


@app.command()
def run(
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", help="Path to YAML run-config"),
) -> None:
    """Read a YAML config file and run backfill or realtime accordingly."""
    if not config.exists():
        raise typer.BadParameter(f"config file not found: {config}")
    rc = load_run_config(config)
    settings.validate()
    _reattach_file_log(_VERBOSE)
    console.print(f"[bold]Loaded config[/] {config}  mode={rc.mode}  data_dir={settings.data_dir}")
    rcfg = _build_restore_cfg(Path(rc.restore_config) if rc.restore_config else None) if rc.restore else None
    if rcfg is not None:
        console.print(f"[bold]Auto-restore[/] enabled  output_dir={rcfg.output_dir}  bbox={rcfg.bbox}")
    if rc.mode == "backfill":
        assert rc.start and rc.end
        _do_backfill(_parse_dt(rc.start), _parse_dt(rc.end), settings.products, rc.dry_run, rcfg)
    else:
        _do_realtime(settings.realtime_window_hours, settings.realtime_interval_sec, rc.once, rcfg)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
