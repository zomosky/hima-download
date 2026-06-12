"""FTP wrapper with reconnect, passive mode, listing, sizing and download."""
from __future__ import annotations

import ftplib
import socket
from pathlib import Path
from typing import Callable, Iterable, Optional

from loguru import logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import settings


_TRANSIENT = (
    ftplib.error_reply,
    socket.timeout,
    ConnectionError,
    EOFError,
    OSError,
)


class RemoteNotFound(Exception):
    """Raised when an FTP path does not exist (450/550)."""


def _is_not_found(err: BaseException) -> bool:
    msg = str(err).lower()
    return "no such file" in msg or "not found" in msg or "failed to open" in msg


def _line_to_entry(line: str) -> tuple[str, int] | None:
    """Parse one line of UNIX-style ``LIST`` output into ``(name, size)``.

    Returns None for directories or unparseable lines.
    """
    parts = line.split(maxsplit=8)
    if len(parts) < 9:
        return None
    perms = parts[0]
    if perms.startswith("d"):
        return None
    try:
        size = int(parts[4])
    except ValueError:
        return None
    name = parts[-1]
    return name, size


class FTPClient:
    """Single-connection FTP wrapper. Not thread-safe; create one per worker."""

    def __init__(self, timeout: int = 60) -> None:
        self.timeout = timeout
        self._ftp: ftplib.FTP | None = None

    # ------------------------------------------------------------------ conn

    def _connect(self) -> ftplib.FTP:
        ftp = ftplib.FTP(settings.ftp_host, timeout=self.timeout)
        ftp.login(settings.ftp_user, settings.ftp_pass)
        ftp.set_pasv(True)
        return ftp

    @property
    def ftp(self) -> ftplib.FTP:
        if self._ftp is None:
            self._ftp = self._connect()
        return self._ftp

    def close(self) -> None:
        if self._ftp is not None:
            try:
                self._ftp.quit()
            except Exception:
                try:
                    self._ftp.close()
                except Exception:
                    pass
            self._ftp = None

    def _reset(self) -> None:
        logger.debug("Resetting FTP connection")
        try:
            if self._ftp is not None:
                self._ftp.close()
        finally:
            self._ftp = None

    # ----------------------------------------------------------------- ops

    @retry(
        retry=retry_if_exception_type(_TRANSIENT),
        stop=stop_after_attempt(settings.max_retries),
        wait=wait_exponential(multiplier=settings.retry_backoff_sec, max=120),
        reraise=True,
    )
    def listdir(self, path: str) -> list[tuple[str, int]]:
        try:
            lines: list[str] = []
            self.ftp.retrlines(f"LIST {path}", lines.append)
        except (ftplib.error_perm, ftplib.error_temp) as e:
            if _is_not_found(e):
                return []
            self._reset()
            raise
        except _TRANSIENT:
            self._reset()
            raise
        out: list[tuple[str, int]] = []
        for ln in lines:
            e = _line_to_entry(ln)
            if e is not None:
                out.append(e)
        return out

    def download(
        self,
        remote: str,
        dest: Path,
        expected_size: int | None = None,
        on_bytes: Optional[Callable[[int], None]] = None,
    ) -> None:
        """Download ``remote`` to ``dest`` atomically (write to .part, then rename).

        ``on_bytes`` is invoked with the chunk size after each block written.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        try:
            with part.open("wb") as f:
                def _writer(chunk: bytes) -> None:
                    f.write(chunk)
                    if on_bytes is not None:
                        on_bytes(len(chunk))
                self.ftp.retrbinary(f"RETR {remote}", _writer, blocksize=1 << 16)
        except (ftplib.error_perm, ftplib.error_temp) as e:
            try:
                part.unlink(missing_ok=True)
            except Exception:
                pass
            if _is_not_found(e):
                raise RemoteNotFound(remote) from e
            self._reset()
            raise
        except _TRANSIENT:
            self._reset()
            try:
                part.unlink(missing_ok=True)
            except Exception:
                pass
            raise
        if expected_size is not None and part.stat().st_size != expected_size:
            part.unlink(missing_ok=True)
            raise OSError(
                f"Size mismatch for {remote}: got {part.stat().st_size}, expected {expected_size}"
            )
        part.replace(dest)

    # --------------------------------------------------------- ctx manager

    def __enter__(self) -> "FTPClient":
        _ = self.ftp  # force connect
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def iter_clients(n: int) -> Iterable[FTPClient]:
    """Yield ``n`` open FTPClient instances; caller is responsible for closing."""
    for _ in range(n):
        yield FTPClient()
