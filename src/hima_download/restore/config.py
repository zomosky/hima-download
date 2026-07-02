"""Restore job config (YAML), mirroring the download project's dataclass+YAML style.

A job YAML is optional: the built-in defaults (China bbox, all three products, zstd Zarr)
work out of the box, and every field can be overridden by a CLI flag.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .catalog import PRODUCTS as ALL_PRODUCTS

BBox = tuple[float, float, float, float]

_VALID_COMPRESSORS = {"zstd", "lz4", "blosclz", "zlib", "none"}
_DEFAULT_WORKERS = min((os.cpu_count() or 1), 4)

# Default chunking favors region time-series reads: the whole time axis in one chunk,
# 256x256 spatial tiles (~12.8 deg at 0.05 deg). Dims absent from a dataset are ignored
# at write time, so one default works across products.
DEFAULT_CHUNKS: dict[str, int] = {"time": -1, "latitude": 256, "longitude": 256}


@dataclass
class RestoreConfig:
    data_dir: Path = Path("data")
    output_dir: Path = Path("zarr")
    bbox: BBox = (70.0, 140.0, 15.0, 55.0)  # west, east, south, north (China)
    products: list[str] = field(default_factory=lambda: list(ALL_PRODUCTS))
    chunks: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_CHUNKS))
    compressor: str = "zstd"  # zstd | lz4 | blosclz | zlib | none
    clevel: int = 3
    consolidated: bool = True
    workers: int = _DEFAULT_WORKERS  # dask threads for parallel read/crop/encode

    def validate(self) -> None:
        bad = [p for p in self.products if p not in ALL_PRODUCTS]
        if bad:
            raise ValueError(f"unknown products {bad}; known: {list(ALL_PRODUCTS)}")
        west, east, south, north = self.bbox
        if not (west < east):
            raise ValueError(f"bbox needs west<east, got west={west} east={east}")
        if not (south < north):
            raise ValueError(f"bbox needs south<north, got south={south} north={north}")
        if self.compressor not in _VALID_COMPRESSORS:
            raise ValueError(
                f"unknown compressor {self.compressor!r}; valid: {sorted(_VALID_COMPRESSORS)}"
            )
        if not (0 <= self.clevel <= 9):
            raise ValueError(f"clevel must be 0..9, got {self.clevel}")
        if self.workers < 1:
            raise ValueError(f"workers must be >=1, got {self.workers}")


def load_config(path: Path) -> RestoreConfig:
    """Load a restore job YAML; unspecified fields keep their defaults."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("config root must be a mapping")
    cfg = RestoreConfig()
    if "data_dir" in raw:
        cfg.data_dir = Path(raw["data_dir"]).expanduser()
    if "output_dir" in raw:
        cfg.output_dir = Path(raw["output_dir"]).expanduser()
    if "bbox" in raw:
        b = raw["bbox"]
        if not isinstance(b, (list, tuple)) or len(b) != 4:
            raise ValueError("bbox must be [west, east, south, north]")
        cfg.bbox = (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
    if "products" in raw:
        cfg.products = [str(p) for p in raw["products"]]
    if "chunks" in raw:
        cfg.chunks = {str(k): int(v) for k, v in dict(raw["chunks"]).items()}
    if "compressor" in raw:
        cfg.compressor = str(raw["compressor"])
    if "clevel" in raw:
        cfg.clevel = int(raw["clevel"])
    if "consolidated" in raw:
        cfg.consolidated = bool(raw["consolidated"])
    if "workers" in raw:
        cfg.workers = int(raw["workers"])
    cfg.validate()
    return cfg
