# map-visualizer

Solves a TSP over every location of a brand in a region and renders the optimal
loop as a 1080x1920 neon animation built for TikTok.

```
pip install -r requirements.txt
python main.py --preview      # 540x960 @ 15fps, ~40s, for checking framing
python main.py                # 1080x1920 @ 30fps, ~5 min
```

Output: `Costco_Texas_tiktok.mp4`, H.264 / yuv420p / +faststart.

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
- `overpassapi.py` — Overpass fetch, cached per query.
- `route.py` — haversine matrix, nearest-neighbour seed, 2-opt + Or-opt.
- `basemap.py` — one-time Cartopy render, cached as PNG + JSON.
- `camera.py` — precomputes the full camera path before rendering.
- `gfx.py` — neon primitives, text, odometer, easing.
- `render.py` — frame compositor, pipes raw frames to ffmpeg.
- `contact_sheet.py` — `python contact_sheet.py out.mp4 1.5 8 20 29` to eyeball frames.

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

## Retargeting

Edit `config.py`:

```python
PLACE_NAME, PLACE_MAIN_TYPE, PLACE_TYPE = "McDonald's", "amenity", "fast_food"
REGION_NAME   = "California"
REGION_EXTENT = [-124.5, -114.1, 32.5, 42.1]
HOOK_LINES    = ["EVERY MCDONALD'S", "IN CALIFORNIA"]
```

Then `python main.py --rebuild-basemap`. Overpass rate-limits, so the first fetch
for a new brand may need a retry; results are cached after that.

## Known trade-offs

- The route is straight-line, not road-routed. "Hours driving" is
  `AVG_SPEED_MPH` against great-circle distance, so treat it as flavour.
- Basemap detail at the widest shot is soft, since one raster is tuned for the
  mid-zoom follow cam. Raise `BASEMAP_PX_WIDE` if that bothers you (memory grows
  roughly with the square).
- No audio track. Add the sound in the TikTok editor — you want a trending sound
  anyway, and the beat drop lands best right at the whip zoom (~2.7s at 30s).
