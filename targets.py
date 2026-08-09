"""Brand presets (Target) and region lookup (resolve_region), the two things
you swap per video. Everything else -- video timing, camera, basemap, theme
-- stays in config.py.

config.retarget() reads TARGETS/REGIONS from here to fill in the derived
C.PLACE_NAME / C.ATP_SPIDER / C.REGION_EXTENT / ... globals every consumer
module already reads.
"""

import json
import os
from dataclasses import dataclass, field

CACHE_DIR = "cache"
REGION_CACHE = os.path.join(CACHE_DIR, "regions.json")


@dataclass(frozen=True)
class Target:
    name: str                          # display name -> PLACE_NAME, headline
    osm: tuple                         # (PLACE_MAIN_TYPE, PLACE_TYPE) Overpass fallback
    atp_spider: str = None             # spider filename minus .py; None -> Overpass only
    atp_brands: frozenset = None       # allow-list of ATP `brand` values; None -> all
    include_instore: bool = False      # keep licensed/in-store locations (ATP only)?
    osm_clauses: tuple = None          # raw Overpass nwr[...] union; None -> single brand/type match
    static_places: tuple = None        # (name, lat, lon) triples; bypasses ATP/Overpass entirely


# Add a brand by adding an entry here -- nothing else needs touching. Find a
# spider name under locations/spiders/ at github.com/alltheplaces/alltheplaces;
# leave atp_spider=None for anything without one (generic amenity queries,
# brands ATP doesn't cover yet) and it falls back to Overpass automatically.
TARGETS = {
    "heb": Target(
        name="H-E-B", osm=("shop", "supermarket"),
        atp_spider="h_e_b_us", atp_brands=frozenset({"H-E-B", "H-E-B plus!"}),
        # H-E-B's OSM data spans multiple brand= strings ("H-E-B" and
        # "H-E-B plus!"/"H-E-B Plus!") plus a handful of untagged stores, so a
        # plain exact match undercounts (304 vs the ~345 stores a viewer would
        # call "an H-E-B"). This clause set is the Overpass fallback path only
        # -- ATP's own store locator doesn't need the workaround. Deliberately
        # excludes Central Market, Joe V's Smart Shop, Mi Tienda (H-E-B-owned
        # but distinct consumer brands) and H-E-B Express (convenience
        # format), and stays away from brand=H-E-B fuel/pharmacy/car_wash
        # POIs, co-located at stores already matched below.
        osm_clauses=(
            'nwr["shop"="supermarket"]["brand"~"^H-E-B( plus!)?$",i]'
            '["name"!~"^Future ",i](area.region);',
            'nwr["shop"="supermarket"]["name"~"^H-?E-?B$",i][!"brand"](area.region);',
        )),
    "whataburger": Target(name="Whataburger", osm=("amenity", "fast_food"),
                         atp_spider="whataburger"),
    "chickfila": Target(name="Chick-fil-A", osm=("amenity", "fast_food"),
                        atp_spider="chick_fil_a"),
    "mcdonalds": Target(name="McDonald's", osm=("amenity", "fast_food"),
                        atp_spider="mcdonalds"),
    'starbucks': Target(
        name='Starbucks', osm=('amenity', 'cafe'),
        atp_spider='starbucks_us'),
    "costco": Target(name="Costco", osm=("shop", "wholesale"),
                     atp_spider="costco_ca_gb_us"),
    # No ATP spider -> always resolves through Overpass. This is the shape a
    # non-chain/generic query takes (no brand=, just an amenity type).
    "plannedparenthood": Target(name="Planned Parenthood", osm=("amenity", "clinic")),
    "highschool": Target(name="High School", osm=("amenity", "school")),
    'raisingcanes': Target(
        name="Raising Cane's", osm=('amenity', 'fast_food'),
        atp_spider='raising_canes_us',
        atp_brands=frozenset({"Raising Cane's"})),
    'sonic': Target(
        name='Sonic', osm=('amenity', 'fast_food'),
        atp_spider='sonic_drivein_us'),
    "flockcameras": Target(
        name="Flock Camera", osm=("man_made", "surveillance"),
        # DeFlock crowd-sources ALPR camera locations into OSM as
        # man_made=surveillance + surveillance:type=ALPR -- covers all ALPR
        # vendors, not just Flock Safety hardware, but that's the common
        # name for the category. No ATP spider; Overpass only. Coverage is
        # volunteer-mapped and likely incomplete -- probe the in-region
        # count before committing to a full render.
        osm_clauses=(
            'nwr["man_made"="surveillance"]["surveillance:type"="ALPR"](area.region);',
        )),
    # No OSM tag for "megachurch" and no ATP spider -- static_places short-
    # circuits places.py's ATP/Overpass dispatch entirely. One point per
    # church (its flagship/primary campus), not every satellite site, for
    # the handful of these that run multiple campuses. Sourced from
    # Wikipedia's "List of megachurches in the United States" (Texas
    # entries) and geocoded one-time via OSM Nominatim; each result was
    # checked to actually land in its stated city before being hardcoded.
    "megachurches": Target(
        name="Megachurch", osm=("amenity", "place_of_worship"),
        static_places=(
            ("Abundant Church", 31.7160803, -106.3112121),
            ("Church Unlimited", 27.6616638, -97.3596366),
            ("Community Bible Church", 29.6035602, -98.4411276),
            ("Fellowship Church", 32.9638564, -97.0324356),
            ("Gateway Church", 32.9497631, -97.1265729),
            ("Green Acres Baptist Church", 32.3204603, -95.2837520),
            ("Hope City Church", 29.8436950, -95.5632800),
            ("Lakewood Church", 29.7303601, -95.4347180),
            ("Lakepointe Church", 32.9797006, -96.2984705),
            ("Milestone Church", 32.9586126, -97.2484964),
            ("One Community Church", 33.1217516, -96.7367308),
            ("Prestonwood Baptist Church", 33.0288229, -96.8463441),
            ("Sagemont Church", 29.5978280, -95.2197785),
            ("The Potter's House", 32.6557377, -96.8861598),
            ("Friendship-West Baptist Church", 32.7048791, -96.8354434),
            ("Inspiring Body of Christ Church", 32.6516051, -96.8893660),
            ("Second Baptist Church", 29.7572394, -95.4989707),
            ("Woodlands Church", 30.2013449, -95.4732195),
        )),
    'chipotle': Target(
        name='Chipotle', osm=('amenity', 'fast_food'),
        atp_spider='chipotle'),
    'home_depot': Target(
        name='Home Depot', osm=('shop', 'doityourself'),
        atp_spider='home_depot',
        atp_brands=frozenset({'The Home Depot'}),
        # OSM tags every real store brand="The Home Depot" (verified live), not
        # the bare "Home Depot" this target displays -- a plain exact match on
        # the Overpass fallback path (auto mode's fallback when ATP is slow/
        # unavailable) matches zero stores. Same class of fix as heb above.
        osm_clauses=(
            'nwr["shop"="doityourself"]["brand"~"^(The )?Home Depot$",i]'
            '["name"!~"^Future ",i](area.region);',
        )),
}

# Hand-tuned overrides for states where the auto-derived bbox (see
# resolve_region below) is wrong or ugly -- Alaska's Aleutians cross the
# antimeridian, so a naive min/max longitude spans nearly the whole globe.
REGIONS = {
    "AK": {"name": "Alaska", "extent": [-170.0, -129.71, 51.3, 71.71]},
}


def _load_region_cache():
    if os.path.exists(REGION_CACHE):
        try:
            with open(REGION_CACHE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_region_cache(cache):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(REGION_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1)


def resolve_region(code, pad_deg):
    """USPS state code -> (name, state_code, extent) where extent is
    [W, E, S, N] in degrees, padded by pad_deg and rounded to 2 decimals so
    repeated calls are cache-stable (basemap.py compares this against JSON'd
    cache metadata to decide whether to rebuild the raster).

    REGIONS entries win outright. Otherwise looked up in the Natural Earth
    admin-1 shapefile Cartopy already downloads for the basemap's state
    outlines, cached to cache/regions.json since a full lookup means parsing
    that shapefile.
    """
    code = code.upper()
    if code in REGIONS:
        r = REGIONS[code]
        return r["name"], code, r["extent"]

    cache_key = f"{code}:{pad_deg}"
    cache = _load_region_cache()
    if cache_key in cache:
        c = cache[cache_key]
        return c["name"], code, c["extent"]

    import cartopy.io.shapereader as shpreader

    shp = shpreader.natural_earth(resolution="50m", category="cultural",
                                  name="admin_1_states_provinces_lakes")
    for rec in shpreader.Reader(shp).records():
        a = rec.attributes
        if a.get("postal") == code and a.get("admin") == "United States of America":
            w, s, e, n = rec.geometry.bounds  # shapely order: minx, miny, maxx, maxy
            extent = [round(w - pad_deg, 2), round(e + pad_deg, 2),
                     round(s - pad_deg, 2), round(n + pad_deg, 2)]
            name = a["name"]
            cache[cache_key] = {"name": name, "extent": extent}
            _save_region_cache(cache)
            return name, code, extent

    raise ValueError(f"no region found for USPS code {code!r} -- add an entry "
                     f"to targets.REGIONS to override manually")
