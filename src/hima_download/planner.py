"""Plan target timelines and resolve them into download jobs."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from .catalog import (
    PRODUCTS,
    Product,
    local_path,
    remote_hour_dir,
    select_files,
)
from .config import settings
from .ftp_client import FTPClient


@dataclass(frozen=True)
class Job:
    product: Product
    timeline: datetime         # UTC, minute-precision
    remote_path: str           # full FTP path of the file
    local_path: Path
    expected_size: int


def iter_timelines(start: datetime, end: datetime, minutes: list[str]) -> list[datetime]:
    """Yield UTC timelines (hour×minutes) between start (incl.) and end (excl.)."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    minute_ints = sorted(int(m) for m in minutes)
    out: list[datetime] = []
    cur = start.replace(minute=0, second=0, microsecond=0)
    while cur < end:
        for m in minute_ints:
            t = cur.replace(minute=m)
            if start <= t < end:
                out.append(t)
        cur += timedelta(hours=1)
    return out


_tls = threading.local()


def _worker_ftp() -> FTPClient:
    c = getattr(_tls, "ftp", None)
    if c is None:
        c = FTPClient()
        _tls.ftp = c
    return c


def _close_worker_ftp(_=None) -> None:
    c = getattr(_tls, "ftp", None)
    if c is not None:
        c.close()
        _tls.ftp = None


def _list_one(pcode: str, hour_dir: str) -> tuple[tuple[str, str], list[tuple[str, int]]]:
    ftp = _worker_ftp()
    try:
        return (pcode, hour_dir), ftp.listdir(hour_dir)
    except Exception as e:
        logger.warning(f"List failed {hour_dir}: {e}")
        return (pcode, hour_dir), []


def plan_jobs(
    client: FTPClient | None,
    products: list[str],
    timelines: list[datetime],
) -> list[Job]:
    """List remote hour-dirs in parallel and emit one Job per matching file.

    The ``client`` parameter is accepted for backward compatibility but unused;
    the planner manages its own connection pool sized by ``settings.concurrency``.
    """
    # 1) Build the unique set of (product, hour_dir) pairs to LIST.
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pcode in products:
        product = PRODUCTS[pcode]
        for t in timelines:
            d = remote_hour_dir(product, t)
            key = (pcode, d)
            if key not in seen:
                seen.add(key)
                pairs.append(key)

    # 2) Parallel LIST with a progress bar.
    cache: dict[tuple[str, str], list[tuple[str, int]]] = {}
    with Progress(
        TextColumn("[bold cyan]Planning"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("hour-dirs"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        transient=False,
    ) as progress:
        task_id = progress.add_task("listing", total=len(pairs))
        with ThreadPoolExecutor(
            max_workers=settings.concurrency,
            thread_name_prefix="hima-list",
        ) as pool:
            futures = [pool.submit(_list_one, p, d) for (p, d) in pairs]
            try:
                for fut in as_completed(futures):
                    key, listing = fut.result()
                    cache[key] = listing
                    progress.update(task_id, advance=1)
            finally:
                close_futs = [pool.submit(_close_worker_ftp) for _ in range(settings.concurrency)]
                for cf in close_futs:
                    try:
                        cf.result(timeout=5)
                    except Exception:
                        pass

    # 3) Assemble jobs from cached listings.
    jobs: list[Job] = []
    for pcode in products:
        product = PRODUCTS[pcode]
        for t in timelines:
            hour_dir = remote_hour_dir(product, t)
            listing = cache.get((pcode, hour_dir)) or []
            if not listing:
                continue
            chosen = select_files(product, t, listing, settings.par_include_japan)
            if not chosen:
                logger.debug(f"No file for {pcode} {t:%Y-%m-%d %H:%M} in {hour_dir}")
                continue
            for name, size in chosen:
                jobs.append(
                    Job(
                        product=product,
                        timeline=t,
                        remote_path=f"{hour_dir}/{name}",
                        local_path=local_path(settings.data_dir, product, t, name),
                        expected_size=size,
                    )
                )
    return jobs


def filter_missing(jobs: list[Job]) -> tuple[list[Job], list[Job]]:
    """Split jobs into (to_download, already_present)."""
    todo: list[Job] = []
    have: list[Job] = []
    for j in jobs:
        p = j.local_path
        if p.exists() and p.stat().st_size == j.expected_size:
            have.append(j)
        else:
            todo.append(j)
    return todo, have
