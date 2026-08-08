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


def build_query(placeName, placeMainType, placeType, region="Texas", queryClauses=None,
                fields=None):
    """Build an Overpass QL query string. `fields` defaults to the pipeline's
    (::id,::lat,::lon,name,brand); pass extras (e.g. amenity/shop/operator)
    for a discovery query that needs to see what tags are actually present.

    queryClauses: optional list of raw Overpass `nwr[...](area.region);`
    statements to union together, for brands whose stores span multiple OSM
    brand= values or include untagged locations (e.g. H-E-B / H-E-B plus!).
    When omitted, falls back to the single exact brand/type match below.
    """
    fields = fields or ["::id", "::lat", "::lon", "name", "brand"]
    field_list = ",".join(fields)

    if queryClauses:
        body = "(\n    " + "\n    ".join(queryClauses) + "\n    );"
    else:
        body = (f'nwr["brand"="{placeName}"]["{placeMainType}"="{placeType}"]'
                f'["name"!="Future {placeName}"](area.region);')

    return f"""
    [out:csv({field_list})][timeout:180];
    area["name"="{region}"]["boundary"="administrative"]["admin_level"="4"]->.region;
    {body}
    out center;
    """


def run_query(query, cache_path, refresh=False, cancel=None):
    """POST `query` to Overpass, or serve it from `cache_path` if present and
    not `refresh`. Cache is keyed by the caller (see _cache_path) on the
    literal query text, so a changed query always refetches.

    `cancel`, if given, is a threading.Event checked before each attempt (not
    mid-request -- a single requests.post() can't be interrupted once it's
    blocking) and again during the backoff wait, mirroring
    main.run_pipeline's between-stages cancel check. Raises
    RuntimeError("cancelled") when set.

    Returns (rows, cached).
    """
    def _check_cancel():
        if cancel is not None and cancel.is_set():
            raise RuntimeError("cancelled")

    os.makedirs(os.path.dirname(cache_path), exist_ok=True)

    if os.path.exists(cache_path) and not refresh:
        with open(cache_path, "r", newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f, delimiter="\t")), True

    last_err = None
    with progress.spinner("fetching from Overpass...") as sp:
        for attempt in range(3):
            _check_cancel()
            try:
                r = requests.post(
                    API_URL,
                    headers={"User-Agent": "map-visualizer (github.com/ZeekWall)"},
                    data={"data": query},
                    timeout=180,
                )
                if r.status_code == 200:
                    with open(cache_path, "w", encoding="utf-8") as f:
                        f.write(r.text)
                    # Written to cache either way (so a retry after a cancel
                    # hits it), but a response that arrives after the user
                    # already gave up on it shouldn't get surfaced as if
                    # nothing happened -- this was the actual gap: the only
                    # other _check_cancel() calls are before an attempt
                    # starts, so a cancel during the single most common case
                    # (the first attempt succeeding) was silently ignored.
                    _check_cancel()
                    return list(csv.DictReader(io.StringIO(r.text), delimiter="\t")), False
                last_err = f"Overpass {r.status_code}: {r.text[:300]}"
            except requests.RequestException as e:
                last_err = str(e)
            if attempt < 2:  # don't sleep after the last attempt, we're about to raise
                wait = 5 * (attempt + 1)  # Overpass rate-limits; back off
                sp.text = f"attempt {attempt + 2}/3 in {wait}s (last: {last_err[:60]})"
                if cancel is not None:
                    cancel.wait(wait)  # wakes early if cancelled instead of sleeping it out
                    _check_cancel()
                else:
                    time.sleep(wait)

    raise RuntimeError(f"Overpass failed after 3 attempts: {last_err}")


def getPlaces(placeName, placeMainType, placeType, region="Texas", refresh=False,
             queryClauses=None):
    """Fetch every OSM node/way/relation for a brand inside an admin area.

    The cache is keyed on the literal query text, so switching PLACE_NAME (or
    queryClauses) actually refetches instead of silently reusing the previous
    brand's file.

    Returns (places, cached).
    """
    query = build_query(placeName, placeMainType, placeType, region, queryClauses)
    path = _cache_path(placeName, region, query)
    return run_query(query, path, refresh=refresh)
