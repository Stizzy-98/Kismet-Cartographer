"""Coordinate validation. The only place that decides whether a point is real.

Kismet writes 0.0/0.0 (packets, data, alerts, messages, device locations) when it had no
GPS fix, and Elasticsearch would happily index that as a valid geo_point at "Null Island".
So (0, 0) is treated as *no fix*, never as a location.

Ordering pitfall: SQL columns are (lat, lon) but Kismet's JSON `geopoint` arrays are
[lon, lat] (GeoJSON order). Elasticsearch arrays are also [lon, lat]; we always emit the
unambiguous object form {"lat": .., "lon": ..} instead.
"""
from __future__ import annotations

import math
from typing import Any, Optional, Tuple

OK, NO_FIX, INVALID = "ok", "no_fix", "invalid"


def classify(lat: Any, lon: Any) -> Tuple[str, Optional[float], Optional[float]]:
    """(status, lat, lon) for an explicit (lat, lon) pair."""
    if lat is None or lon is None:
        return NO_FIX, None, None
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return INVALID, None, None
    if math.isnan(la) or math.isnan(lo) or math.isinf(la) or math.isinf(lo):
        return INVALID, None, None
    if la == 0.0 and lo == 0.0:
        return NO_FIX, None, None
    if not (-90.0 <= la <= 90.0) or not (-180.0 <= lo <= 180.0):
        return INVALID, None, None
    return OK, la, lo


def from_kismet_geopoint(gp: Any) -> Tuple[str, Optional[float], Optional[float]]:
    """Kismet JSON geopoint is [lon, lat]."""
    if not isinstance(gp, (list, tuple)) or len(gp) < 2:
        return NO_FIX, None, None
    return classify(gp[1], gp[0])


def num(v: Any) -> Optional[float]:
    """float(v) or None (bool and non-finite excluded)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def km_to_box(lat: float, lon: float, box) -> float:
    """Great-circle distance (km) from a point to the nearest point of a (min_lat, max_lat, min_lon, max_lon)
    box; 0 inside it."""
    la = min(max(lat, box[0]), box[1])
    lo = min(max(lon, box[2]), box[3])
    p = math.pi / 180.0
    a = math.sin((lat - la) * p / 2) ** 2 + math.cos(lat * p) * math.cos(la * p) * math.sin((lon - lo) * p / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(min(1.0, a)))
