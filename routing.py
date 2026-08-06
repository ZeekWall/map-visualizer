"""Road-routed leg geometry via a self-hosted OSRM instance.

Fetches driving directions for consecutive stops in the solved tour (the TSP
order itself is still solved on the haversine matrix in route.py — this only
replaces the straight-line geometry and distance/duration of each leg).

See scripts/osrm-setup.sh for standing up OSRM locally.
"""

import hashlib
import json
import math
import os
import time

import numpy as np
import requests

import config as C
import progress


def _cache_path(coords, closed, cache_dir):
    key = (np.round(coords[closed], 6).tobytes() +
           f"{C.OSRM_PROFILE}{C.ROAD_SIMPLIFY_DEG}".encode())
    h = hashlib.sha1(key).hexdigest()[:12]
    return os.path.join(cache_dir, f"{h}.json")


def _haversine_km(a, b):
    R = 6371.0088
    lat1, lon1 = np.radians(a[0]), np.radians(a[1])
    lat2, lon2 = np.radians(b[0]), np.radians(b[1])
    dphi, dlam = lat2 - lat1, lon2 - lon1
    x = np.sin(dphi / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlam / 2) ** 2
    return float(2 * R * np.arcsin(np.sqrt(np.clip(x, 0, 1))))


def _straight_leg(a, b):
    """Fallback leg: 2-point segment, haversine distance, no duration."""
    return {"verts": [[a[0], a[1]], [b[0], b[1]]],
            "km": _haversine_km(a, b), "sec": None}


def _simplify(verts, tol_deg):
    """Cheap cumulative-distance filter. Always keeps the first/last vertex."""
    if len(verts) <= 2:
        return verts
    out = [verts[0]]
    for v in verts[1:-1]:
        last = out[-1]
        if abs(v[0] - last[0]) + abs(v[1] - last[1]) >= tol_deg:
            out.append(v)
    out.append(verts[-1])
    return out


def _request_chunk(coord_chunk):
    """coord_chunk: list of [lat, lon]. Returns the raw OSRM /route response."""
    coord_str = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in coord_chunk)
    url = f"{C.OSRM_URL}/route/v1/{C.OSRM_PROFILE}/{coord_str}"
    params = {
        "overview": "full",
        "geometries": "geojson",
        "steps": "true",
        "continue_straight": "false",
    }
    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=60)
            if r.status_code == 200:
                return r.json()
            last_err = f"OSRM {r.status_code}: {r.text[:300]}"
        except requests.RequestException as e:
            last_err = str(e)
        if attempt < 2:  # don't sleep after the last attempt, we're about to raise
            wait = 5 * (attempt + 1)
            with progress.spinner(f"OSRM request failed, retrying ({attempt + 2}/3) in {wait}s..."):
                time.sleep(wait)
    raise RuntimeError(
        f"OSRM request failed after 3 attempts ({last_err}). "
        f"Is OSRM running at {C.OSRM_URL}? See scripts/osrm-setup.sh."
    )


def _legs_from_chunk(resp, chunk_pts):
    """Extract one Leg dict per consecutive pair in chunk_pts from an OSRM response."""
    n_expected = len(chunk_pts) - 1
    if resp.get("code") != "Ok" or len(resp.get("routes", [])) == 0:
        return [None] * n_expected

    route = resp["routes"][0]
    osrm_legs = route.get("legs", [])
    if len(osrm_legs) != n_expected:
        return [None] * n_expected

    out = []
    for i, leg in enumerate(osrm_legs):
        coords = []
        for step in leg.get("steps", []):
            coords.extend(step["geometry"]["coordinates"])  # [lon, lat]
        if not coords:
            out.append(None)
            continue
        verts = [[lat, lon] for lon, lat in coords]  # -> [lat, lon]
        # snap endpoints exactly to the requested stops
        verts[0] = list(chunk_pts[i])
        verts[-1] = list(chunk_pts[i + 1])
        out.append({
            "verts": _simplify(verts, C.ROAD_SIMPLIFY_DEG),
            "km": leg["distance"] / 1000.0,
            "sec": leg["duration"],
        })
    return out


def fetch_legs(coords, closed, cache_dir=None, refresh=False):
    """coords: (n, 2) [lat, lon]. closed: tour indices, first == last (closed loop).

    Returns a list of Leg dicts, one per consecutive pair in `closed`:
      {"verts": [[lat, lon], ...], "km": float, "sec": float|None}
    """
    cache_dir = cache_dir or C.ROAD_CACHE
    os.makedirs(cache_dir, exist_ok=True)
    path = _cache_path(coords, closed, cache_dir)

    if os.path.exists(path) and not refresh:
        with open(path) as f:
            legs = json.load(f)
        total_verts = sum(len(l["verts"]) for l in legs)
        total_km = sum(l["km"] for l in legs)
        progress.done(f"{total_verts:,} road vertices, {total_km:,.0f} km (cached)")
        return legs

    pts = [coords[i] for i in closed]
    n_legs = len(pts) - 1

    if not C.ROUTING_ENABLED:
        legs = [_straight_leg(pts[i], pts[i + 1]) for i in range(n_legs)]
        with open(path, "w") as f:
            json.dump(legs, f)
        progress.done(f"{n_legs} straight legs (routing disabled)")
        return legs

    legs = [None] * n_legs
    batch = max(2, C.ROAD_BATCH)
    fallback_count = 0
    # chunks overlap by one coordinate to stay continuous, so each chunk of
    # `batch` points covers batch-1 legs
    n_chunks = max(1, math.ceil(n_legs / (batch - 1)))
    bar = progress.bar(n_chunks, unit=" chunks")

    i = 0
    chunk_no = 0
    while i < len(pts) - 1:
        # overlap by one point so chunk boundaries stay continuous
        chunk = pts[i:i + batch]
        if len(chunk) < 2:
            break
        resp = _request_chunk(chunk)
        chunk_legs = _legs_from_chunk(resp, chunk)
        for j, leg in enumerate(chunk_legs):
            legs[i + j] = leg
        i += len(chunk) - 1
        chunk_no += 1
        bar.update(chunk_no)

    for idx, leg in enumerate(legs):
        if leg is None:
            fallback_count += 1
            if not C.ROUTE_FALLBACK_STRAIGHT:
                raise RuntimeError(f"Unroutable leg {idx} and ROUTE_FALLBACK_STRAIGHT is off.")
            legs[idx] = _straight_leg(pts[idx], pts[idx + 1])

    total_verts = sum(len(l["verts"]) for l in legs)
    total_km = sum(l["km"] for l in legs)
    summary = f"{total_verts:,} road vertices, {total_km:,.0f} km"
    if fallback_count:
        summary += f"  ({fallback_count}/{n_legs} legs fell back to straight lines)"
    progress.done(summary)

    with open(path, "w") as f:
        json.dump(legs, f)
    return legs
