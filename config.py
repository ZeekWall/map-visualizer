"""All the knobs. Edit this file, re-run main.py.

Retargeting to a new brand/region is two lines -- TARGET and REGION below.
Everything under "resolved" is generated from those two by retarget();
don't hand-edit it, it'll just get overwritten. Add a new brand in
targets.py, not here. Everything under "how it looks" is unaffected by
TARGET/REGION and safe to tune freely.
"""

import targets

# ================================================================== target
TARGET = "heb"                 # key into targets.TARGETS
REGION = "TX"                  # USPS state code

PLACE_SOURCE = "auto"          # auto | atp | osm -- see targets.py for what
                               # "auto" tries first and when it falls back
REGION_PAD_DEG = 0.3           # padding added to the auto-derived region bbox


# ================================================================ resolved
# Filled in by retarget(); every consumer module reads these names exactly
# as before. Re-run retarget() (main.py does, after parsing --target/--region)
# any time TARGET/REGION/PLACE_SOURCE/REGION_PAD_DEG change.
def retarget(target=None, region=None):
    global TARGET, REGION, PLACE_NAME, PLACE_MAIN_TYPE, PLACE_TYPE, \
        PLACE_QUERY_CLAUSES, ATP_SPIDER, ATP_BRANDS, ATP_INCLUDE_INSTORE, \
        REGION_NAME, REGION_STATE, REGION_EXTENT, HOOK_LINES

    TARGET = target or TARGET
    REGION = region or REGION

    t = targets.TARGETS[TARGET]
    PLACE_NAME = t.name
    PLACE_MAIN_TYPE, PLACE_TYPE = t.osm
    PLACE_QUERY_CLAUSES = list(t.osm_clauses) if t.osm_clauses else None
    ATP_SPIDER = t.atp_spider
    ATP_BRANDS = t.atp_brands
    ATP_INCLUDE_INSTORE = t.include_instore

    REGION_NAME, REGION_STATE, REGION_EXTENT = targets.resolve_region(REGION, REGION_PAD_DEG)

    HOOK_LINES = [f"EVERY {PLACE_NAME.upper()}", f"IN {REGION_NAME.upper()}"]


retarget()


# =============================================================== how it looks
# ---------------------------------------------------------------- video
# 1440x2560 by default -- above 1080p gives TikTok's re-encoder a cleaner
# source (this content is near-worst-case for their transcoder: near-black bg,
# thin neon lines, smooth glow gradients -> banding). --res on the CLI can
# drop to 1080 or push to 2160 for a slow, maximum-quality master.
OUT_W, OUT_H = 1440, 2560      # 9:16
FPS = 30
DURATION_SEC = 61              # 15 / 30 / 60 all work
CRF = 18                       # 18 = visually lossless-ish, 20-23 = smaller file
PRESET = "medium"
OUT_FILE = None                # None -> "<Place>_<Region>_tiktok.mp4"
OUT_DIR = "out"                 # rendered posts land in out/<Brand>_<Region>/

# ---------------------------------------------------------------- act timing
# Fractions of total runtime. Must sum to 1.0.
ACT_HOOK = 0.03                # loop already lit and breathing, copy on screen
ACT_WHIP = 0.035               # zoom to the start pin while the route un-draws to dim
ACT_DRIVE = 0.795              # follow-cam along the route
ACT_REVEAL = 0.14              # pull back out, full loop + final stats

# the whole lit loop breathes during the hook instead of a fast sweep (which
# read as flicker at the wide shot, not motion) -- CYCLES must be a whole or
# half-integer so the breath starts AND ends at rest (sin(0) = sin(n*pi) = 0),
# matching the reveal's resting brightness at both ends of the hook
HOOK_PULSE_CYCLES = 3.0    # full brighten/dim cycles across the hook
HOOK_PULSE_GAIN   = 0.45   # peak brightness above rest
HOOK_PULSE_DIP    = 0.22   # trough below rest (asymmetric: swells more than it dips)
HOOK_PULSE_BLOOM  = 0.9    # extra glow-only swell at peak, on top of GAIN

# ---------------------------------------------------------------- camera
# Zoom is derived from how far the camera travels in LOOKAHEAD_SEC, so on-screen
# speed stays roughly constant whether it's 45 Costcos or 1,300 McDonald's, and
# whether the video is 15s or 60s. The MIN/MAX are only guard rails.
LOOKAHEAD_SEC = .5            # seconds of road visible ahead; lower = tighter/faster
ZOOM_MIN_DEG = 0.30
ZOOM_MAX_DEG = 6
ZOOM_RESPONSE = 1.0            # >1 exaggerates the tight/wide contrast
CAMERA_SMOOTH_SEC = 0.75       # gaussian smoothing on the camera path
WIDE_SHOT_LIFT = 0.20          # pushes the map up in wide shots to clear the stats block
DISTANCE_WEIGHT = 0.65         # 1.0 = constant km/s, 0.0 = constant stops/s

# ---------------------------------------------------------------- basemap
BASEMAP_PX_WIDE = 6000         # per 1080p of OUT_W; scaled up at higher --res
BASEMAP_CACHE = "cache/basemap"
DRAW_ROADS = True              # Natural Earth 10m roads (slow first render)
CITY_MIN_POP = 10_000          # label pool floor; only affects what's cached

# City labels fade in/out on a population threshold that slides with zoom.
# Both ends are anchored in log space, since population and half-width each
# span orders of magnitude.
CITY_LABEL_POP_WIDE  = 1_200_000   # threshold at the widest (hook/reveal) shot
CITY_LABEL_POP_TIGHT = 10_000      # threshold at ZOOM_MIN_DEG
CITY_LABEL_FADE_DECADES = 0.25     # log10 band a label fades across
CITY_LABEL_MAX = 14                # hard cap on labels drawn per frame
CITY_LABEL_SIZE_MIN, CITY_LABEL_SIZE_MAX = 26, 38   # authored px, by population

# ---------------------------------------------------------------- copy
HOOK_SUB = "one perfect loop"
REVEAL_LINE = "THE FULL LOOP"
USE_MILES = True
AVG_SPEED_MPH = 62             # for the "hours of driving" payoff stat
GAS_COST_PER_MILE = 0.18       # blended "typical car" estimate; edit to taste

# ---------------------------------------------------------------- solver
SOLVER_TIME_BUDGET = 45        # seconds for 2-opt / Or-opt improvement
ROUTE_CACHE = "cache/route"

# ---------------------------------------------------------------- routing
# Road-routed legs via a self-hosted OSRM instance (see scripts/osrm-setup.sh).
# When disabled, legs fall back to straight great-circle segments (old behaviour).
ROUTING_ENABLED = True
OSRM_URL = "http://localhost:5000"
OSRM_PROFILE = "driving"
ROAD_CACHE = "cache/roads"
ROAD_BATCH = 50                 # coords per OSRM /route request
ROAD_SIMPLIFY_DEG = 0.0005      # ~55m; drop vertices closer together than this
ROUTE_FALLBACK_STRAIGHT = True  # unroutable leg -> straight segment instead of aborting
TAIL_KM = 35                    # arc-length of the bright neon tail behind the head

# ---------------------------------------------------------------- stop accent
# Every stop hit fires its own expanding ring, independent of and overlapping
# with any other still-live ring -- this is lifetime, not a rate limit.
STOP_RING_SEC = 0.55            # lifetime of one ring, start to full fade

# ---------------------------------------------------------------- loop
LOOP_SEAMLESS = True   # dissolve the reveal so the last frame matches frame 0
LOOP_FADE_SEC = 0.6    # length of that dissolve

# ---------------------------------------------------------------- theme
BG          = (5, 7, 15)
LAND        = (11, 16, 32)
LAND_EDGE   = (30, 42, 74)
WATER       = (7, 12, 26)
ROAD        = (19, 28, 51)

NEON_CORE   = (170, 250, 255)   # bright inner line
NEON_MID    = (0, 214, 255)     # cyan
NEON_OUTER  = (255, 43, 214)    # magenta bloom
HEAD_COLOR  = (255, 255, 255)

DOT_VISITED = (240, 0, 0)
DOT_PENDING = (153, 0, 0)
TEXT        = (255, 255, 255)
TEXT_DIM    = (150, 170, 205)
ACCENT      = (255, 43, 214)

# TikTok UI safe zones (px). Nothing important goes here.
SAFE_TOP = 180
SAFE_BOTTOM = 430              # caption + username strip
SAFE_RIGHT = 190               # like/comment/share rail
TEXT_MARGIN = 60               # min gutter each side of headline text
