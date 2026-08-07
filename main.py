import argparse
import os
import re

import numpy as np

import basemap
import camera
import config as C
import progress
import render
import route
import routing
from overpassapi import getPlaces


def load_coords(refresh=False):
    places, cached = getPlaces(C.PLACE_NAME, C.PLACE_MAIN_TYPE, C.PLACE_TYPE,
                               C.REGION_NAME, refresh=refresh)
    pts = []
    for p in places:
        try:
            lat, lon = float(p["@lat"]), float(p["@lon"])
        except (TypeError, ValueError, KeyError):
            continue
        if np.isfinite(lat) and np.isfinite(lon):
            pts.append((lat, lon))
    coords = np.array(pts)
    if len(coords) < 3:
        raise SystemExit(f"Only {len(coords)} valid locations — nothing to animate.")
    # OSM often has a drive-thru node and a building way for the same store
    _, keep = np.unique(np.round(coords, 4), axis=0, return_index=True)
    coords = coords[np.sort(keep)]
    tag = " (cached)" if cached else ""
    progress.done(f"{len(coords)} {C.PLACE_NAME} locations in {C.REGION_NAME}{tag}")
    return coords


def assemble_path(coords, tour, refresh_roads=False):
    """Turn the solved stop order into road-routed (or straight, if routing is
    disabled) vertex geometry.

    Returns:
      lons, lats, cum_dist  - per road-vertex, parallel arrays (cum_dist in km)
      stop_vert             - stop_vert[s] = vertex index of stop s
      stop_dist             - cum_dist[stop_vert], i.e. per-stop cumulative km
      total_hours           - summed OSRM leg duration, or None if unavailable
    """
    closed = list(tour) + [tour[0]]
    legs = routing.fetch_legs(coords, closed, refresh=refresh_roads)

    verts = []
    stop_vert = [0]
    for leg in legs:
        lv = leg["verts"][1:] if verts else leg["verts"]
        verts.extend(lv)
        stop_vert.append(len(verts) - 1)
    stop_vert = np.array(stop_vert)

    ordered = np.array(verts, dtype=np.float64)
    lats, lons = ordered[:, 0], ordered[:, 1]

    # raw per-vertex haversine cumulative distance, then rescaled leg-by-leg to
    # match OSRM's reported road km (keeps the odometer honest while cum_dist
    # stays monotonic, which camera.build / render both rely on)
    raw = np.zeros(len(ordered))
    if len(ordered) > 1:
        lat1, lat2 = np.radians(lats[:-1]), np.radians(lats[1:])
        dlat, dlon = np.radians(np.diff(lats)), np.radians(np.diff(lons))
        a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
        seg_km = 2 * 6371.0088 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
        raw[1:] = np.cumsum(seg_km)

    cum_dist = np.zeros(len(ordered))
    for s, leg in enumerate(legs):
        v0, v1 = stop_vert[s], stop_vert[s + 1]
        raw_len = raw[v1] - raw[v0]
        scale = leg["km"] / raw_len if raw_len > 1e-9 else 1.0
        cum_dist[v0:v1 + 1] = cum_dist[v0] + (raw[v0:v1 + 1] - raw[v0]) * scale

    stop_dist = cum_dist[stop_vert]
    secs = [leg["sec"] for leg in legs]
    total_hours = sum(secs) / 3600.0 if all(s is not None for s in secs) else None

    return lons, lats, cum_dist, stop_vert, stop_dist, total_hours


def output_paths(preview):
    """out/<Brand>_<Region>/tiktok[_preview].{mp4,png} -- one folder per post so
    the video and its cover stay together. C.OUT_FILE overrides the path."""
    if C.OUT_FILE:
        base = re.sub(r"\.mp4$", "", C.OUT_FILE)
    else:
        slug = "{}_{}".format(
            re.sub(r"[^A-Za-z0-9]+", "", C.PLACE_NAME),
            re.sub(r"[^A-Za-z0-9]+", "", C.REGION_NAME))
        base = os.path.join(C.OUT_DIR, slug, "tiktok")
    if preview:
        base += "_preview"
    d = os.path.dirname(base)
    if d:
        os.makedirs(d, exist_ok=True)
    return base + ".mp4", base + "_cover.png"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-places", action="store_true")
    ap.add_argument("--refresh-roads", action="store_true")
    ap.add_argument("--rebuild-basemap", action="store_true")
    ap.add_argument("--seconds", type=float, default=None)
    ap.add_argument("--preview", action="store_true",
                    help="540x960 @ 15fps for a fast look")
    ap.add_argument("--quiet", action="store_true",
                    help="disable progress output")
    ap.add_argument("--cover-only", action="store_true",
                    help="write just the cover PNG, skip video encode")
    args = ap.parse_args()

    if args.quiet:
        progress.enabled = False

    if args.seconds:
        C.DURATION_SEC = args.seconds
    if args.preview:
        C.OUT_W, C.OUT_H, C.FPS, C.CRF, C.PRESET = 540, 960, 15, 26, "veryfast"

    progress.step(1, 6, "Places")
    coords = load_coords(refresh=args.refresh_places)

    progress.step(2, 6, "Route")
    tour, D, length = route.solve(coords, C.SOLVER_TIME_BUDGET, C.ROUTE_CACHE)

    progress.step(3, 6, "Roads")
    lons, lats, cum_dist, stop_vert, stop_dist, total_hours = assemble_path(
        coords, tour, refresh_roads=args.refresh_roads)

    progress.step(4, 6, "Basemap")
    img, meta, cities = basemap.build(force=args.rebuild_basemap)

    progress.step(5, 6, "Camera")
    cam = camera.build(lons, lats, cum_dist, C.REGION_EXTENT, stop_dist)
    progress.done(f"{cam['n']} frames planned")

    out, cover = output_paths(args.preview)

    progress.step(6, 6, "Cover" if args.cover_only else "Render")
    render.render(lons, lats, cum_dist, stop_vert, stop_dist, total_hours,
                  cam, img, meta, cities, out,
                  cover_path=cover, cover_only=args.cover_only)
    print(f"Done: {os.path.abspath(cover if args.cover_only else out)}")


if __name__ == "__main__":
    main()
