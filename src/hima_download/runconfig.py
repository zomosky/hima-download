"""YAML run-config: drives mode selection and overrides Settings fields."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

import yaml

from .config import settings

Mode = Literal["backfill", "realtime"]

# Fields in Settings that may be overridden via YAML.
_OVERRIDABLE = {
    "data_dir",
    "concurrency",
    "products",
    "minutes",
    "par_include_japan",
    "realtime_window_hours",
    "realtime_interval_sec",
    "max_retries",
    "retry_backoff_sec",
    "log_file",
    "log_rotation",
    "log_retention",
}


@dataclass
class RunConfig:
    mode: Mode = "backfill"
    # backfill range (UTC strings, parsed by cli._parse_dt)
    start: Optional[str] = None
    end: Optional[str] = None
    dry_run: bool = False
    # realtime
    once: bool = False
    # raw overrides to apply to global settings
    overrides: dict[str, Any] = field(default_factory=dict)


def load_run_config(path: Path) -> RunConfig:
    """Load a YAML run-config and apply Settings overrides as a side-effect."""
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise RuntimeError(f"Config root must be a mapping, got {type(raw).__name__}")

    mode = str(raw.get("mode", "backfill")).lower()
    if mode not in ("backfill", "realtime"):
        raise RuntimeError(f"config.mode must be 'backfill' or 'realtime', got {mode!r}")

    overrides: dict[str, Any] = {}
    for k in _OVERRIDABLE:
        if k in raw:
            overrides[k] = raw[k]

    rc = RunConfig(mode=mode, overrides=overrides)  # type: ignore[arg-type]

    if mode == "backfill":
        bf = raw.get("backfill") or {}
        rc.start = bf.get("start")
        rc.end = bf.get("end")
        rc.dry_run = bool(bf.get("dry_run", False))
        if not rc.start or not rc.end:
            raise RuntimeError("backfill mode requires backfill.start and backfill.end")
    else:
        rt = raw.get("realtime") or {}
        rc.once = bool(rt.get("once", False))
        if "window_hours" in rt:
            overrides["realtime_window_hours"] = int(rt["window_hours"])
        if "interval_sec" in rt:
            overrides["realtime_interval_sec"] = int(rt["interval_sec"])

    settings.apply_overrides(overrides)
    return rc
