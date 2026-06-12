"""Concurrent downloader: one FTP connection per worker thread."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from loguru import logger
from rich.progress import (
    BarColumn,
    DownloadColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from .config import settings
from .ftp_client import FTPClient, RemoteNotFound
from .planner import Job


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


@dataclass
class DownloadResult:
    ok: int = 0
    failed: int = 0
    bytes: int = 0


def _download_with_retry(job: Job, on_bytes) -> tuple[bool, int, str | None]:
    """Run one download with manual retries; rewind progress between attempts."""
    ftp = _worker_ftp()
    acc = 0  # bytes advanced on the progress bar for this attempt

    def _cb(n: int) -> None:
        nonlocal acc
        acc += n
        on_bytes(n)

    last_err: str | None = None
    for attempt in range(1, settings.max_retries + 1):
        try:
            ftp.download(job.remote_path, job.local_path,
                         expected_size=job.expected_size, on_bytes=_cb)
            return True, job.expected_size, None
        except RemoteNotFound as e:
            on_bytes(-acc)  # rewind
            return False, 0, f"NOT FOUND: {e}"
        except Exception as e:
            last_err = f"{job.remote_path} (attempt {attempt}/{settings.max_retries}): {e}"
            on_bytes(-acc)
            acc = 0
            if attempt < settings.max_retries:
                backoff = min(settings.retry_backoff_sec * (2 ** (attempt - 1)), 120)
                logger.warning(f"{last_err}; retry in {backoff}s")
                time.sleep(backoff)
    return False, 0, last_err


def run_jobs(jobs: list[Job]) -> DownloadResult:
    """Run download jobs concurrently with one FTP connection per worker."""
    result = DownloadResult()
    if not jobs:
        return result

    total_bytes = sum(j.expected_size for j in jobs)
    started = time.monotonic()
    lock = threading.Lock()

    with Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        transient=False,
    ) as progress:
        task_id = progress.add_task(f"Downloading {len(jobs)} files", total=total_bytes)

        def _on_bytes(n: int) -> None:
            with lock:
                progress.update(task_id, advance=n)

        with ThreadPoolExecutor(
            max_workers=settings.concurrency,
            thread_name_prefix="hima-dl",
        ) as pool:
            futures = [pool.submit(_download_with_retry, j, _on_bytes) for j in jobs]
            try:
                for fut in as_completed(futures):
                    ok, sz, err = fut.result()
                    if ok:
                        result.ok += 1
                        result.bytes += sz
                    else:
                        result.failed += 1
                        logger.error(err or "unknown download error")
            finally:
                close_futs = [pool.submit(_close_worker_ftp) for _ in range(settings.concurrency)]
                for cf in close_futs:
                    try:
                        cf.result(timeout=5)
                    except Exception:
                        pass

    elapsed = time.monotonic() - started
    mbps = (result.bytes / 1e6) / elapsed if elapsed > 0 else 0.0
    logger.info(
        f"Done: ok={result.ok} failed={result.failed} "
        f"size={result.bytes/1e9:.2f} GB in {elapsed:.1f}s ({mbps:.1f} MB/s)"
    )
    return result
