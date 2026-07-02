"""Restore sub-package: crop downloaded Himawari L2 NetCDF and write per-(product, month) Zarr.

This is the downstream of the FTP downloader (:mod:`hima_download`). It never touches the
network: it globs the flat ``<data_dir>/<PRODUCT>/<YYYYMM>/*.nc`` tree the downloader writes,
crops each frame to a lon/lat bbox (China by default), concatenates a month along a new
``time`` axis, and writes one compact Zarr store per (product, month).

The CLI entry point is :func:`hima_download.restore.cli.main` (console script ``hima-restore``).
Importing this package is deliberately light; the heavy xarray/zarr imports live in
:mod:`hima_download.restore.convert`.
"""

from __future__ import annotations

__all__: list[str] = []
