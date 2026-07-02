"""Tests for bbox cropping on a Himawari-like grid (descending lat, 0-360 lon)."""

from __future__ import annotations

import numpy as np
import xarray as xr

from hima_download.restore.crop import crop_bbox


def _himawari_like():
    """Tiny grid mimicking the 2401 product: lat 60->-60 descending, lon 80->200."""
    lat = np.arange(60.0, -60.01, -5.0)  # descending
    lon = np.arange(80.0, 200.01, 5.0)  # ascending, 0-360 convention
    data = np.arange(lat.size * lon.size, dtype="int16").reshape(lat.size, lon.size)
    return xr.Dataset(
        {"AOT": (("latitude", "longitude"), data)},
        coords={"latitude": lat, "longitude": lon},
    )


def test_crop_china_bbox_nonempty_and_in_range():
    ds = _himawari_like()
    out = crop_bbox(ds, (70.0, 140.0, 15.0, 55.0))  # west,east,south,north
    assert out.sizes["latitude"] > 0 and out.sizes["longitude"] > 0
    assert float(out.latitude.min()) >= 15.0 and float(out.latitude.max()) <= 55.0
    # west=70 is left of the grid start (80), so longitudes clip to [80, 140]
    assert float(out.longitude.min()) >= 80.0 and float(out.longitude.max()) <= 140.0


def test_crop_preserves_descending_latitude():
    ds = _himawari_like()
    out = crop_bbox(ds, (90.0, 150.0, 20.0, 50.0))
    lat = out.latitude.values
    assert lat[0] > lat[-1]  # still descending, not silently emptied


def test_crop_no_coords_passthrough():
    ds = xr.Dataset({"x": ("a", np.arange(3))})
    assert crop_bbox(ds, (70.0, 140.0, 15.0, 55.0)) is ds
