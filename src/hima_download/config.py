"""Configuration loaded from environment / .env (with optional YAML overrides)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()


def _env_str(key: str, default: str) -> str:
    return os.getenv(key, default)


def _env_int(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))


def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_list(key: str, default: list[str]) -> list[str]:
    v = os.getenv(key)
    if not v:
        return default
    return [x.strip() for x in v.split(",") if x.strip()]


@dataclass
class Settings:
    ftp_host: str = field(default_factory=lambda: _env_str("HIMA_FTP_HOST", "ftp.ptree.jaxa.jp"))
    ftp_user: str = field(default_factory=lambda: _env_str("HIMA_FTP_USER", ""))
    ftp_pass: str = field(default_factory=lambda: _env_str("HIMA_FTP_PASS", ""))

    data_dir: Path = field(default_factory=lambda: Path(_env_str("HIMA_DATA_DIR", "./data")).resolve())
    concurrency: int = field(default_factory=lambda: _env_int("HIMA_CONCURRENCY", 4))

    products: list[str] = field(default_factory=lambda: _env_list("HIMA_PRODUCTS", ["PAR", "CLP", "ARP"]))
    minutes: list[str] = field(default_factory=lambda: _env_list("HIMA_MINUTES", ["00", "10"]))
    par_include_japan: bool = field(default_factory=lambda: _env_bool("HIMA_PAR_INCLUDE_JAPAN", False))

    realtime_window_hours: int = field(default_factory=lambda: _env_int("HIMA_REALTIME_WINDOW_HOURS", 6))
    realtime_interval_sec: int = field(default_factory=lambda: _env_int("HIMA_REALTIME_INTERVAL_SEC", 300))

    max_retries: int = field(default_factory=lambda: _env_int("HIMA_MAX_RETRIES", 5))
    retry_backoff_sec: int = field(default_factory=lambda: _env_int("HIMA_RETRY_BACKOFF_SEC", 10))
    # Socket timeout for FTP control/data ops. Default 60s is fine for recent data
    # (~1MB/s), but JAXA's archived (old-month) retrieval stalls >60s and times out;
    # raise (e.g. HIMA_FTP_TIMEOUT=300) for historical backfill so slow transfers finish.
    ftp_timeout: int = field(default_factory=lambda: _env_int("HIMA_FTP_TIMEOUT", 60))

    log_file: Path = field(default_factory=lambda: Path(_env_str("HIMA_LOG_FILE", "./logs/hima-download.log")).resolve())
    log_rotation: str = field(default_factory=lambda: _env_str("HIMA_LOG_ROTATION", "50 MB"))
    log_retention: str = field(default_factory=lambda: _env_str("HIMA_LOG_RETENTION", "14 days"))

    def apply_overrides(self, d: dict[str, Any]) -> None:
        """Apply a flat dict of overrides; keys map 1:1 to fields."""
        for k, v in d.items():
            if v is None or not hasattr(self, k):
                continue
            cur = getattr(self, k)
            if isinstance(cur, Path):
                v = Path(v).expanduser().resolve()
            setattr(self, k, v)

    def validate(self) -> None:
        if not self.ftp_user or not self.ftp_pass:
            raise RuntimeError(
                "HIMA_FTP_USER / HIMA_FTP_PASS not set. Copy .env.example to .env and fill in credentials."
            )
        bad = [p for p in self.products if p not in {"PAR", "CLP", "ARP"}]
        if bad:
            raise RuntimeError(f"Unknown products in HIMA_PRODUCTS: {bad}")
        bad_min = [m for m in self.minutes if not (len(m) == 2 and m.isdigit() and 0 <= int(m) < 60 and int(m) % 10 == 0)]
        if bad_min:
            raise RuntimeError(f"Invalid HIMA_MINUTES entries (must be 2-digit, multiples of 10): {bad_min}")


settings = Settings()
