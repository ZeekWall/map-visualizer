# map-visualizer

Solves a TSP over every location of a brand in a region and renders the optimal
loop as a 1080x1920 neon animation built for TikTok.

```
pip install -r requirements.txt
python main.py --preview      # 540x960 @ 15fps, ~40s, for checking framing
python main.py                # 1080x1920 @ 30fps, ~5 min
python main.py --cover-only   # just the cover PNG, no video encode, ~seconds
```

Legs are road-routed by default via a self-hosted OSRM instance (see
[Road routing](#road-routing) below). Set `ROUTING_ENABLED = False` in
`config.py` to fall back to straight great-circle legs and skip the OSRM
dependency entirely.

Output lands in `out/<Brand>_<Region>/`, one folder per post:

```
out/Costco_Texas/
├─ tiktok.mp4              H.264 / yuv420p / +faststart
├─ tiktok_cover.png        establishing shot, title + stop count settled
├─ tiktok_preview.mp4      from --preview
└─ tiktok_preview_cover.png
```

`cache/`, `shots/`, `osrm/`, and `out/` are all generated/downloaded and safe to
delete — everything in them is rebuilt on the next run (network calls aside).

## Shot structure

| Act | Default | What happens |
|---|---|---|
| Hook | 9% | Wide state shot, stops ignite, headline slams in, total stop count |
| Whip | 5% | Eased zoom down to the first pin |
| Drive | 72% | Follow-cam, live mileage odometer, per-stop shockwave rings |
| Reveal | 14% | Pull back to the finished loop, final stat block |

Change the split with `ACT_*` in `config.py` — they must sum to 1.0.

## Files

- `config.py` — every knob. Brand, region, colours, timing, copy.
- `places.py` — dispatches to All The Places (preferred) or Overpass (fallback).
- `atpapi.py` — All The Places fetch, cached per spider/region/run.
- `overpassapi.py` — Overpass fetch, cached per query.
- `route.py` — haversine matrix, nearest-neighbour seed, 2-opt + Or-opt. Still
  solves stop *order* on great-circle distance — only the drawn geometry and
  distance/duration come from roads.
- `routing.py` — road-routed leg geometry via OSRM, cached per tour.
- `basemap.py` — one-time Cartopy render, cached as PNG + JSON.
- `camera.py` — precomputes the full camera path before rendering.
- `gfx.py` — neon primitives, text, odometer, easing.
- `render.py` — frame compositor, pipes raw frames to ffmpeg.
- `contact_sheet.py` — `python contact_sheet.py out.mp4 1.5 8 20 29` to eyeball frames.

## Road routing

Stop *order* is still solved on the haversine matrix (fast, no network calls
for 1,300+ stops). Once the tour is fixed, `routing.py` fetches real road
geometry, distance, and duration for each leg from a self-hosted
[OSRM](https://project-osrm.org/) instance — the neon line follows actual
highways instead of cutting straight across the map, and "MI DRIVEN" / "HOURS
DRIVING" report real numbers instead of great-circle estimates.

One-time setup (needs Docker):

```
scripts/osrm-setup.sh north-america/us/texas
```

This downloads a Geofabrik `.osm.pbf` extract into `./osrm/`, builds the OSRM
graph, and serves it on `http://localhost:5000`. The extract must cover
`REGION_NAME`/`REGION_EXTENT` in `config.py` — switching states means
re-running the script against a new extract (see
[download.geofabrik.de](https://download.geofabrik.de) for available
regions). Leave OSRM running and just `python main.py` as usual; results are
cached per-tour in `cache/roads/`, use `--refresh-roads` to force a refetch.

Relevant knobs in `config.py`: `ROUTING_ENABLED`, `OSRM_URL`, `OSRM_PROFILE`,
`ROAD_BATCH`, `ROAD_SIMPLIFY_DEG`, `ROUTE_FALLBACK_STRAIGHT`, `TAIL_KM`.

## What changed from the original

**Portrait math.** The old `FIG_ASPECT = 9/16` was applied as
`half_h = half_w * FIG_ASPECT`, which squashes the camera box in a vertical
frame. The camera now derives `half_h` from the output aspect *and* `cos(lat)`,
so distances are not stretched at the top of Texas relative to the bottom.

**The basemap renders once.** `ax.set_extent()` inside the animation callback
made Cartopy reproject 10m North America roads on every frame. Cartopy now runs
a single time at 6000px wide and caches to disk; each frame is a `cv2.warpAffine`
crop out of a mip pyramid. That is the difference between hours and ~5 minutes.

**Additive neon instead of a plotted line.** Glow is real bloom — a blurred mask
tinted magenta and cyan, composited additively at half resolution, with a crisp
core line and a hot flare at the leading edge. Compositing stays in float32 for
the whole frame and clips once at the end.

**Speed-adaptive camera.** Zoom is derived from how far the camera travels in
`LOOKAHEAD_SEC`, so long highway legs pull out and metro clusters pull in, and
on-screen speed stays constant whether it is 45 Costcos or 1,300 McDonald's, at
15s or 60s. The path is Gaussian-smoothed in log-zoom space so nothing snaps.

**Fixed the cache bug.** `overpassapi.py` returned `places.csv` whenever the file
existed, regardless of brand — so `PLACE_NAME = "McDonald's"` was silently
rendering cached Costco data. The cache key is now a hash of the full query.

**Encoder.** `bitrate=2000` at 1080x1920 was far too low. Now CRF 18, yuv420p,
High@4.1, `+faststart`.

**Solver.** Replaced `python_tsp.solve_tsp_local_search` with candidate-neighbour
2-opt + Or-opt. The old best-improvement search is O(n^2) per move and cannot
touch a 1,300-node problem in 60s; this stays interactive and caches its result.

## Layout notes

The HUD is authored against a 1080x1920 canvas and scaled by `OUT_W / 1080`, so
`--preview` is a true miniature of the final render. `SAFE_BOTTOM` (430px) and
`SAFE_RIGHT` (190px) keep text clear of TikTok's caption strip and button rail.
`WIDE_SHOT_LIFT` pushes the map into the upper two thirds during the hook and
reveal so the stat block never lands on the route.

## Fonts

Falls back through Anton → Bebas Neue → Inter Black → Poppins Bold → DejaVu Bold.
Drop `Anton-Regular.ttf` into `./fonts/` for the condensed look most of these
videos use — Poppins is rounder and reads softer at large sizes.

## Data source

Brand POIs come from **All The Places** (alltheplaces.xyz) when a spider is
configured, falling back to Overpass/OSM otherwise (`PLACE_SOURCE = "auto"` in
`config.py`, set to `"atp"` or `"osm"` to force one). ATP scrapes ~4,950
brands' own store-locator APIs weekly and republishes as CC0 GeoJSON — no
attribution required, and since it doesn't go through OSM's community tagging
it doesn't inherit OSM's inconsistent `brand=` values. That inconsistency is
exactly what `PLACE_QUERY_CLAUSES` below works around for OSM, and why viewer
reports of missing locations mostly go away once a spider covers the brand.
Measured against this repo's cached Overpass results for Texas:

| brand | OSM | ATP | delta |
|---|---:|---:|---:|
| H-E-B | 343 | 345 | +2 (already hand-tuned via `PLACE_QUERY_CLAUSES`) |
| Whataburger | 730 | 773 | +43 |
| Chick-fil-A | 448 | 522 | +74 |
| McDonald's | 1147 | 1260 | +113 |

To use ATP for a brand, find its spider filename (minus `.py`) under
`locations/spiders/` in [github.com/alltheplaces/alltheplaces](https://github.com/alltheplaces/alltheplaces)
and set `ATP_SPIDER` in `config.py`. `ATP_BRANDS` filters co-located
sub-brands a spider may also emit (e.g. `h_e_b_us` also returns
`"H-E-B Pharmacy"` rows); `ATP_INCLUDE_INSTORE` controls whether
licensed/in-store locations count (e.g. a Starbucks counter inside a Target —
944 standalone vs 1471 including those, in Texas). Leave `ATP_SPIDER = None`
to always use Overpass.

Attribution still applies: Natural Earth (public domain) backs the basemap
and OSRM/OSM backs road routing, so `© OpenStreetMap contributors` belongs in
the video description regardless of which source supplies the POIs.

## Retargeting

Edit `config.py`:

```python
PLACE_NAME, PLACE_MAIN_TYPE, PLACE_TYPE = "McDonald's", "amenity", "fast_food"
PLACE_QUERY_CLAUSES = None     # reset unless the new brand also needs a union query
ATP_SPIDER    = "mcdonalds"    # None -> always use Overpass instead
ATP_BRANDS    = None
REGION_NAME   = "California"
REGION_STATE  = "CA"
REGION_EXTENT = [-124.5, -114.1, 32.5, 42.1]
HOOK_LINES    = ["EVERY MCDONALD'S", "IN CALIFORNIA"]
```

Then `python main.py --rebuild-basemap`. Overpass rate-limits, so the first
Overpass fetch for a new brand may need a retry; results are cached after
that either way.

`PLACE_QUERY_CLAUSES` overrides Overpass's default single brand/type match
with a union of raw `nwr[...]` statements — for brands like H-E-B whose OSM
stores span multiple `brand=` values or include untagged locations (see the
block above `REGION_NAME` in `config.py`). It's Overpass-only and has no
effect when `ATP_SPIDER` is set and resolves data. Leave it `None` for the
common single-brand case, and remember to reset both it and `ATP_SPIDER` when
retargeting away from a brand that set them, or the new brand will silently
inherit the old query/spider.

## Known trade-offs

- Stop *order* is still solved on great-circle distance, not road distance —
  see [Road routing](#road-routing). For most regions the ordering barely
  changes; a true road-distance solve would need an OSRM `/table` matrix,
  which doesn't scale past a few hundred stops without chunking.
- With `ROUTING_ENABLED = False` (or no OSRM reachable), legs fall back to
  straight great-circle segments and "hours driving" reverts to
  `AVG_SPEED_MPH` against that distance — treat it as flavour in that mode.
- Basemap detail at the widest shot is soft, since one raster is tuned for the
  mid-zoom follow cam. Raise `BASEMAP_PX_WIDE` if that bothers you (memory grows
  roughly with the square).
- No audio track. Add the sound in the TikTok editor — you want a trending sound
  anyway, and the beat drop lands best right at the whip zoom (~2.7s at 30s).
