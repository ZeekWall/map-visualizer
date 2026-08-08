import csv
import hashlib
import io
import os
import re
import time

import requests

import progress

CACHE_DIR = "cache/places"
API_URL = "https://overpass-api.de/api/interpreter"


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _cache_path(place_name, region, query):
    h = hashlib.sha1(query.encode()).hexdigest()[:8]
    return os.path.join(CACHE_DIR, f"{_slug(place_name)}-{_slug(region)}-{h}.csv")


def getPlaces(placeName, placeMainType, placeType, region="Texas", refresh=False,
             queryClauses=None):
    """Fetch every OSM node/way/relation for a brand inside an admin area.

    The cache is keyed on the literal query text, so switching PLACE_NAME (or
    queryClauses) actually refetches instead of silently reusing the previous
    brand's file.

    queryClauses: optional list of raw Overpass `nwr[...](area.region);`
    statements to union together, for brands whose stores span multiple OSM
    brand= values or include untagged locations (e.g. H-E-B / H-E-B plus!).
    When omitted, falls back to the single exact brand/type match below.

    Returns (places, cached).
    """
    os.makedirs(CACHE_DIR, exist_ok=True)

    if queryClauses:
        body = "(\n    " + "\n    ".join(queryClauses) + "\n    );"
    else:
        body = (f'nwr["brand"="{placeName}"]["{placeMainType}"="{placeType}"]'
                f'["name"!="Future {placeName}"](area.region);')

    query = f"""
    [out:csv(::id,::lat,::lon,name,brand)][timeout:180];
    area["name"="{region}"]["boundary"="administrative"]["admin_level"="4"]->.region;
    {body}
    out center;
    """

    path = _cache_path(placeName, region, query)

    if os.path.exists(path) and not refresh:
        with open(path, "r", newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f, delimiter="\t")), True

    last_err = None
    with progress.spinner("fetching from Overpass...") as sp:
        for attempt in range(3):
            try:
                r = requests.post(
                    API_URL,
                    headers={"User-Agent": "map-visualizer (github.com/ZeekWall)"},
                    data={"data": query},
                    timeout=180,
                )
                if r.status_code == 200:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(r.text)
                    return list(csv.DictReader(io.StringIO(r.text), delimiter="\t")), False
                last_err = f"Overpass {r.status_code}: {r.text[:300]}"
            except requests.RequestException as e:
                last_err = str(e)
            if attempt < 2:  # don't sleep after the last attempt, we're about to raise
                wait = 5 * (attempt + 1)  # Overpass rate-limits; back off
                sp.text = f"attempt {attempt + 2}/3 in {wait}s (last: {last_err[:60]})"
                time.sleep(wait)

    raise RuntimeError(f"Overpass failed after 3 attempts: {last_err}")
