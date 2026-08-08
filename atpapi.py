import csv
import json
import os
import re
import time

import requests

import progress

CACHE_DIR = "cache/places"
INDEX_URL = "https://data.alltheplaces.xyz/runs/latest/info_embed.html"
DATA_HOST = "https://alltheplaces-data.openaddresses.io"
RUN_ID_FILE = os.path.join(CACHE_DIR, "atp_run.txt")
RUN_ID_TTL_SEC = 24 * 3600

_FIELDS = ["@id", "@lat", "@lon", "name", "brand"]


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _resolve_run_id(refresh=False):
    """Return the current ATP run id (e.g. "2026-08-01-13-32-15"), cached for
    RUN_ID_TTL_SEC so a normal invocation doesn't re-hit the index page.
    """
    if not refresh and os.path.exists(RUN_ID_FILE):
        age = time.time() - os.path.getmtime(RUN_ID_FILE)
        if age < RUN_ID_TTL_SEC:
            with open(RUN_ID_FILE, "r", encoding="utf-8") as f:
                run_id = f.read().strip()
            if run_id:
                return run_id

    r = requests.get(INDEX_URL, timeout=30,
                     headers={"User-Agent": "map-visualizer (github.com/ZeekWall)"})
    r.raise_for_status()
    m = re.search(r"runs/([0-9T:-]+)/output\.zip", r.text)
    if not m:
        raise RuntimeError("could not find a run id in All The Places' index page")
    run_id = m.group(1)

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(RUN_ID_FILE, "w", encoding="utf-8") as f:
        f.write(run_id)
    return run_id


def _cache_path(spider, region, run_id):
    return os.path.join(CACHE_DIR, f"atp-{_slug(spider)}-{_slug(region)}-{run_id}.csv")


def _in_bbox(lat, lon, extent):
    if extent is None:
        return True
    w, e, s, n = extent
    return w <= lon <= e and s <= lat <= n


def _fetch_spider_geojson(spider):
    """Download and return the parsed feature list for one spider's latest run.
    Retries like overpassapi.getPlaces: 3 attempts, linear backoff.
    """
    run_id = _resolve_run_id()
    url = f"{DATA_HOST}/runs/{run_id}/output/{spider}.geojson"

    last_err = None
    with progress.spinner("fetching from All The Places...") as sp:
        for attempt in range(3):
            try:
                r = requests.get(
                    url,
                    headers={"User-Agent": "map-visualizer (github.com/ZeekWall)"},
                    timeout=180,
                )
                if r.status_code == 200:
                    return json.loads(r.text)["features"], run_id
                last_err = f"ATP {r.status_code}: {r.text[:300]}"
            except (requests.RequestException, json.JSONDecodeError) as e:
                last_err = str(e)
            if attempt < 2:
                wait = 5 * (attempt + 1)
                sp.text = f"attempt {attempt + 2}/3 in {wait}s (last: {last_err[:60]})"
                time.sleep(wait)

    raise RuntimeError(f"All The Places fetch failed after 3 attempts: {last_err}")


def getPlaces(spider, region_state, region_extent, brands=None, include_instore=True,
             refresh=False):
    """Fetch every location for one All The Places spider, filtered to a state
    and normalized to the same row shape overpassapi.getPlaces returns
    (@id, @lat, @lon, name, brand), so main.py's parsing is unaffected.

    brands: optional set of allowed `brand` values (drops co-located
    sub-brands the spider also emits, e.g. "H-E-B Pharmacy").
    include_instore: when False, drops rows that look like a licensed /
    inside-another-store location (ownership_type == "LS", or a `located_in`
    value set).

    Returns (places, cached).
    """
    os.makedirs(CACHE_DIR, exist_ok=True)

    run_id = None
    if not refresh:
        # Reuse an already-cached CSV for the current run id without hitting
        # the network at all, mirroring overpassapi's cache-hit path.
        try:
            run_id = _resolve_run_id()
        except (requests.RequestException, RuntimeError):
            run_id = None
        if run_id:
            path = _cache_path(spider, region_state or "all", run_id)
            if os.path.exists(path):
                with open(path, "r", newline="", encoding="utf-8") as f:
                    return list(csv.DictReader(f, delimiter="\t")), True

    features, run_id = _fetch_spider_geojson(spider)

    rows = []
    for feat in features:
        props = feat.get("properties") or {}
        geom = feat.get("geometry") or {}
        if geom.get("type") != "Point":
            continue
        coords = geom.get("coordinates")
        if not coords or len(coords) < 2:
            continue
        lon, lat = coords[0], coords[1]

        brand = props.get("brand")
        if brands is not None and brand not in brands:
            continue

        if region_state and props.get("addr:state") != region_state:
            continue
        if not _in_bbox(lat, lon, region_extent):
            continue

        if not include_instore:
            if props.get("ownership_type") == "LS":
                continue
            if props.get("located_in"):
                continue

        rows.append({
            "@id": props.get("ref") or feat.get("id") or "",
            "@lat": lat,
            "@lon": lon,
            "name": props.get("name") or "",
            "brand": brand or "",
        })

    path = _cache_path(spider, region_state or "all", run_id)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_FIELDS, delimiter="\t")
        w.writeheader()
        w.writerows(rows)

    return rows, False
