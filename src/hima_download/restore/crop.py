"""Bbox cropping for Himawari's regular lat/lon grid.

Himawari JAXA gridded products are a regular equirectangular grid with **descending
latitude** (60 -> -60) and a **native 0-360 longitude axis** (e.g. 80-200 for the
2401 grid, 70-210 for PAR-2801). Cropping is therefore a plain ``.sel`` slice -- no
geostationary resampling -- but two traps must be respected:

* descending latitude => the slice must go high -> low;
* the China default bbox (70-140) sits in the positive 0-360 range, so we slice it
  directly and deliberately do **not** rewrap longitudes to -180..180 (which would
  break the monotonic axis for the western-Pacific extent).
"""

from __future__ import annotations

import xarray as xr

BBox = tuple[float, float, float, float]  # west, east, south, north


def crop_bbox(ds: xr.Dataset, bbox: BBox) -> xr.Dataset:
    """Crop ``ds`` to ``bbox`` = (west, east, south, north) in degrees.

    Returns ``ds`` unchanged if it has no ``latitude``/``longitude`` coords.
    """
    if "latitude" not in ds.coords or "longitude" not in ds.coords:
        return ds
    west, east, south, north = bbox
    lat = ds["latitude"].values
    descending = lat.size >= 2 and float(lat[0]) > float(lat[-1])
    lat_slice = slice(north, south) if descending else slice(south, north)
    return ds.sel(latitude=lat_slice, longitude=slice(west, east))
