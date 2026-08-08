"""Discovery logic backing the TUI's Explore tab: "how many locations does
this brand have, and under what OSM tags / ATP spider" -- the research loop
that used to happen entirely outside the tool (by hand, with a browser and a
copy of Overpass Turbo) before landing as a hand-written Target() entry.

No Textual import here, on purpose -- same split as knobs.py backing the
Knobs tab. targets_edit.py (writing the result into targets.py) is a
separate module for the same reason knobs.py and targets.py are separate:
discovery/rendering logic shouldn't know how its caller displays it.
"""

import json
import os
import re
import time
from dataclasses import dataclass, field

import requests

import atpapi
import overpassapi
import progress
import textnorm

CACHE_DIR = "cache/explore"
SPIDER_INDEX_FILE = os.path.join(CACHE_DIR, "atp_spiders.json")
SPIDER_INDEX_TTL_SEC = 7 * 24 * 3600
SPIDER_TREE_URL = ("https://api.github.com/repos/alltheplaces/alltheplaces/"
                   "git/trees/master?recursive=1")

SAMPLE_SIZE = 20


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


@dataclass
class OsmProbe:
    query_text: str
    total: int
    by_type: list       # [(main, type, count), ...] descending by count
    by_brand: list       # [(brand_or_empty, count), ...] descending by count
    rows: list = field(default_factory=list)  # first SAMPLE_SIZE raw rows
    cached: bool = False


def osm_probe(query_text, region_name, refresh=False, cancel=None):
    """One Overpass call for a free-text brand/name guess: matches either an
    exact `brand=` value or a `name=` prefix (untagged locations included),
    and asks for amenity/shop/operator so the caller can see what OSM type
    the real stores actually carry instead of guessing.

    `cancel`, if given, is a threading.Event forwarded to
    overpassapi.run_query -- see its docstring for what "cancel" actually
    covers (between attempts, not mid-request).
    """
    # fuzzy_regex widens apostrophe/dash characters to match either the
    # straight or typographic variant -- OSM's community-edited brand= tags
    # don't reliably agree with what a user types (see textnorm.py), and a
    # strict match here would just silently return nothing for e.g.
    # "McDonald's" if the actual tag uses "McDonald's".
    q = textnorm.fuzzy_regex(query_text.strip())
    clauses = [
        f'nwr["brand"~"^{q}$",i](area.region);',
        f'nwr["name"~"^{q}",i][!"brand"](area.region);',
    ]
    fields = ["::id", "::lat", "::lon", "name", "brand", "amenity", "shop", "operator"]
    query = overpassapi.build_query(query_text, "", "", region_name,
                                    queryClauses=clauses, fields=fields)

    os.makedirs(CACHE_DIR, exist_ok=True)
    h = __import__("hashlib").sha1(query.encode()).hexdigest()[:10]
    cache_path = os.path.join(CACHE_DIR, f"osm-{_slug(query_text)}-{_slug(region_name)}-{h}.csv")

    rows, cached = overpassapi.run_query(query, cache_path, refresh=refresh, cancel=cancel)

    type_counts = {}
    brand_counts = {}
    for r in rows:
        main = "amenity" if r.get("amenity") else ("shop" if r.get("shop") else "")
        typ = r.get("amenity") or r.get("shop") or ""
        type_counts[(main, typ)] = type_counts.get((main, typ), 0) + 1
        brand = r.get("brand") or ""
        brand_counts[brand] = brand_counts.get(brand, 0) + 1

    by_type = sorted(([m, t, c] for (m, t), c in type_counts.items()),
                     key=lambda x: -x[2])
    by_type = [(m, t, c) for m, t, c in by_type]
    # Merge apostrophe/dash-variant spellings so e.g. 400 "McDonald's" and
    # 12 "McDonald's" show up as one row with the true total, instead of
    # splitting the count (and, worse, letting the user select only one
    # spelling and think they've captured every store).
    by_brand = sorted(textnorm.merge_variants(brand_counts).items(), key=lambda x: -x[1])

    return OsmProbe(query_text=query, total=len(rows), by_type=by_type,
                    by_brand=by_brand, rows=rows[:SAMPLE_SIZE], cached=cached)


def _load_spider_index(refresh=False, cancel=None):
    if not refresh and os.path.exists(SPIDER_INDEX_FILE):
        age = time.time() - os.path.getmtime(SPIDER_INDEX_FILE)
        if age < SPIDER_INDEX_TTL_SEC:
            try:
                with open(SPIDER_INDEX_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

    if cancel is not None and cancel.is_set():
        raise RuntimeError("cancelled")

    try:
        r = requests.get(SPIDER_TREE_URL, timeout=30,
                         headers={"User-Agent": "map-visualizer (github.com/ZeekWall)"})
        r.raise_for_status()
        tree = r.json()
        if tree.get("truncated"):
            progress.warn("All The Places spider index came back truncated")
        names = sorted(
            os.path.splitext(os.path.basename(t["path"]))[0]
            for t in tree.get("tree", [])
            if t["path"].startswith("locations/spiders/") and t["path"].endswith(".py")
        )
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(SPIDER_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(names, f)
        # Written to cache either way, but see overpassapi.run_query's
        # matching comment -- a cancel that landed while this single
        # request was in flight shouldn't get silently overridden by a
        # result that arrives right after.
        if cancel is not None and cancel.is_set():
            raise RuntimeError("cancelled")
        return names
    except (requests.RequestException, ValueError) as e:
        # Network hiccup or GitHub's 60/hr unauthenticated rate limit --
        # fall back to whatever's cached, even if stale, rather than fail
        # outright.
        if os.path.exists(SPIDER_INDEX_FILE):
            progress.warn(f"All The Places spider index fetch failed ({e}), using stale cache")
            with open(SPIDER_INDEX_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        raise RuntimeError(f"could not fetch or find a cached spider index: {e}")


def search_spiders(query_text, refresh=False, limit=15, cancel=None):
    """Rank All The Places spider filenames against a free-text brand guess.
    ATP publishes no queryable spider index -- the run manifest
    (runs/latest/info_embed.html) only carries a run id and a row count --
    so this ranks against the spider filenames themselves, pulled from the
    alltheplaces/alltheplaces GitHub tree (cached SPIDER_INDEX_TTL_SEC).
    """
    names = _load_spider_index(refresh=refresh, cancel=cancel)
    # ATP's own slugs drop an apostrophe entirely rather than treating it
    # as a separator -- "Raising Cane's" -> "raising_canes_us", not
    # "raising_cane_s_us" -- so strip it the same way before collapsing
    # the rest of the punctuation to underscores. Otherwise a query typed
    # with the apostrophe (the natural way to type it) inserts a token
    # boundary that was never in the real spider name and the exact/
    # startswith/prefix branches below all miss it, same as the
    # token-overlap fallback (it demands tokens.issubset(name_tokens),
    # and the spurious "s" token isn't a subset of anything).
    cleaned = textnorm.strip_apostrophes(query_text.strip().lower())
    norm = re.sub(r"[^a-z0-9]+", "_", cleaned).strip("_")
    tokens = set(norm.split("_"))

    scored = []
    for name in names:
        if name == norm:
            score = 100
        elif name == f"{norm}_us":
            # This app only covers US regions, and a lot of brands ATP
            # covers (Starbucks, McDonald's, ...) have a spider per
            # country -- without this, `_us` ties with every other
            # country suffix at the score below and can lose the
            # alphabetical tiebreak entirely once there are enough of them
            # to spill past `limit` (Starbucks alone has 18: `_us` sorts
            # after `_th`, so it never made the cut).
            score = 95
        elif name.startswith(norm + "_"):
            score = 90  # e.g. whataburger -> whataburger_us
        elif name.startswith(norm):
            score = 70
        else:
            name_tokens = set(name.split("_"))
            overlap = tokens & name_tokens
            if not overlap or not tokens.issubset(name_tokens):
                continue
            score = 40 + 5 * len(overlap)
        scored.append((score, name))

    scored.sort(key=lambda x: (-x[0], x[1]))
    return [name for _, name in scored[:limit]]


@dataclass
class AtpProbe:
    spider: str
    total: int
    by_brand: list        # [(brand_or_empty, count), ...] descending
    instore_count: int    # rows that include_instore=False would drop
    rows: list = field(default_factory=list)  # first SAMPLE_SIZE normalized rows


def atp_probe(spider, region_state, region_extent, cancel=None):
    """Download and tally one ATP spider's full run -- the multi-MB request
    overpassapi's discovery query avoids, so this only runs when the user
    explicitly asks to probe a specific spider candidate.
    """
    features, _run_id = atpapi._fetch_spider_geojson(spider, cancel=cancel)

    brand_counts = {}
    instore = 0
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

        if region_state and props.get("addr:state") != region_state:
            continue
        if not atpapi._in_bbox(lat, lon, region_extent):
            continue

        brand = props.get("brand") or ""
        brand_counts[brand] = brand_counts.get(brand, 0) + 1
        if props.get("ownership_type") == "LS" or props.get("located_in"):
            instore += 1

        if len(rows) < SAMPLE_SIZE:
            rows.append({"@id": props.get("ref") or feat.get("id") or "",
                        "@lat": lat, "@lon": lon,
                        "name": props.get("name") or "", "brand": brand})

    # Same merge as osm_probe -- a spider's own feed can carry a brand
    # value with inconsistent punctuation across records too.
    by_brand = sorted(textnorm.merge_variants(brand_counts).items(), key=lambda x: -x[1])
    total = sum(c for _, c in by_brand)
    return AtpProbe(spider=spider, total=total, by_brand=by_brand,
                    instore_count=instore, rows=rows)
