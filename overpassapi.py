import csv
import hashlib
import io
import os
import re
import time

import requests

CACHE_DIR = "cache/places"
API_URL = "https://overpass-api.de/api/interpreter"


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _cache_path(place_name, main_type, place_type, region):
    key = f"{place_name}|{main_type}|{place_type}|{region}"
    h = hashlib.sha1(key.encode()).hexdigest()[:8]
    return os.path.join(CACHE_DIR, f"{_slug(place_name)}-{_slug(region)}-{h}.csv")


def getPlaces(placeName, placeMainType, placeType, region="Texas", refresh=False):
    """Fetch every OSM node/way/relation for a brand inside an admin area.

    The cache is keyed on the full query, so switching PLACE_NAME actually
    refetches instead of silently reusing the previous brand's file.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(placeName, placeMainType, placeType, region)

    if os.path.exists(path) and not refresh:
        with open(path, "r", newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f, delimiter="\t"))

    query = f"""
    [out:csv(::id,::lat,::lon,name,brand)][timeout:180];
    area["name"="{region}"]["boundary"="administrative"]["admin_level"="4"]->.region;
    nwr["brand"="{placeName}"]["{placeMainType}"="{placeType}"]["name"!="Future {placeName}"](area.region);
    out center;
    """

    last_err = None
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
                return list(csv.DictReader(io.StringIO(r.text), delimiter="\t"))
            last_err = f"Overpass {r.status_code}: {r.text[:300]}"
        except requests.RequestException as e:
            last_err = str(e)
        time.sleep(5 * (attempt + 1))  # Overpass rate-limits; back off

    raise RuntimeError(f"Overpass failed after 3 attempts: {last_err}")
