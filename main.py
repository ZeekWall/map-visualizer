import argparse
import os
import re

import numpy as np

import basemap
import camera
import config as C
import render
import route
from overpassapi import getPlaces


def load_coords(refresh=False):
    places = getPlaces(C.PLACE_NAME, C.PLACE_MAIN_TYPE, C.PLACE_TYPE,
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
    print(f"{len(coords)} {C.PLACE_NAME} locations in {C.REGION_NAME}")
    return coords


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-places", action="store_true")
    ap.add_argument("--rebuild-basemap", action="store_true")
    ap.add_argument("--seconds", type=float, default=None)
    ap.add_argument("--preview", action="store_true",
                    help="540x960 @ 15fps for a fast look")
    args = ap.parse_args()

    if args.seconds:
        C.DURATION_SEC = args.seconds
    if args.preview:
        C.OUT_W, C.OUT_H, C.FPS, C.CRF, C.PRESET = 540, 960, 15, 26, "veryfast"

    coords = load_coords(refresh=args.refresh_places)
    tour, D, length = route.solve(coords, C.SOLVER_TIME_BUDGET, C.ROUTE_CACHE)

    closed = list(tour) + [tour[0]]
    ordered = coords[closed]
    lats, lons = ordered[:, 0], ordered[:, 1]
    seg = D[closed[:-1], closed[1:]]
    cum_dist = np.concatenate([[0.0], np.cumsum(seg)])

    img, meta, cities = basemap.build(force=args.rebuild_basemap)
    cam = camera.build(lons, lats, cum_dist, C.REGION_EXTENT)

    out = C.OUT_FILE or "{}_{}_tiktok.mp4".format(
        re.sub(r"[^A-Za-z0-9]+", "", C.PLACE_NAME),
        re.sub(r"[^A-Za-z0-9]+", "", C.REGION_NAME))
    if args.preview:
        out = out.replace(".mp4", "_preview.mp4")

    print(f"Rendering {cam['n']} frames at {C.OUT_W}x{C.OUT_H}...")
    render.render(lons, lats, cum_dist, cam, img, meta, cities, out)
    print(f"Done: {os.path.abspath(out)}")


if __name__ == "__main__":
    main()
