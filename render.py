"""Frame compositor. Crops the cached basemap for the current camera, paints the
neon route on top, lays out the HUD, and pipes raw frames straight into ffmpeg."""

import subprocess
import time

import cv2
import numpy as np

import config as C
import gfx
import progress


KM_PER_MI = 1.609344

# UI is authored against a 1080x1920 canvas and scaled to whatever OUT_W is,
# so --preview looks exactly like the final render, just smaller.
_S = 1.0


def S(v):
    return v * _S


class Viewport:
    """Maps lon/lat <-> screen px and pulls the matching basemap crop.

    Keeps a mip pyramid so a wide shot resamples from a small level instead of
    downscaling 6000px every frame.
    """

    def __init__(self, img, meta, levels=4):
        self.extent = meta["extent"]
        self.bw, self.bh = meta["width"], meta["height"]
        self.pyr = [img]
        for _ in range(levels - 1):
            self.pyr.append(cv2.pyrDown(self.pyr[-1]))

    def bounds(self, clon, clat, hw):
        half_h = hw * (C.OUT_H / C.OUT_W) * np.cos(np.radians(clat))
        return clon - hw, clon + hw, clat - half_h, clat + half_h

    def project(self, lons, lats, cam):
        lon0, lon1, lat0, lat1 = cam
        x = (np.asarray(lons) - lon0) / (lon1 - lon0) * C.OUT_W
        y = (lat1 - np.asarray(lats)) / (lat1 - lat0) * C.OUT_H
        return x, y

    def crop(self, cam):
        W, E, S, N = self.extent
        lon0, lon1, lat0, lat1 = cam
        sx = self.bw / (E - W)
        sy = self.bh / (N - S)
        px0, px1 = (lon0 - W) * sx, (lon1 - W) * sx
        py0, py1 = (N - lat1) * sy, (N - lat0) * sy

        factor = (px1 - px0) / C.OUT_W
        lvl = int(np.clip(round(np.log2(max(factor, 1e-6))), 0, len(self.pyr) - 1))
        f = 2 ** lvl
        src = self.pyr[lvl]

        a = (px1 - px0) / f / C.OUT_W
        b = (py1 - py0) / f / C.OUT_H
        M = np.array([[a, 0, px0 / f], [0, b, py0 / f]], np.float32)
        return cv2.warpAffine(
            src, M, (C.OUT_W, C.OUT_H),
            flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
            borderMode=cv2.BORDER_CONSTANT, borderValue=C.BG,
        )


def fmt_int(v):
    return f"{int(round(v)):,}"


def fmt_money(v):
    return f"${int(round(v)):,}"


def render(lons, lats, cum_dist, stop_vert, stop_dist, total_hours,
          cam, basemap_img, meta, cities, out_path):
    """lons/lats/cum_dist are per road-vertex (dense). stop_vert[s] is the
    vertex index of stop s; stop_dist is cum_dist[stop_vert]. When routing is
    disabled every vertex is a stop, so stop_vert == arange(n) and the two
    index spaces collapse back to the old behaviour."""
    global _S
    _S = C.OUT_W / 1080.0
    vp = Viewport(basemap_img, meta)
    n_frames = cam["n"]
    total_km = cum_dist[-1]
    n_stops = len(stop_dist) - 1
    unit = "MI" if C.USE_MILES else "KM"
    conv = (1 / KM_PER_MI) if C.USE_MILES else 1.0

    vignette = gfx.make_vignette(C.OUT_W, C.OUT_H)

    # vert_idx: which road-vertex segment the head is currently on (for path
    # geometry). stop_no: which store has most recently been visited (for the
    # HUD counter and the pending/visited dot masks).
    vert_idx = np.clip(np.searchsorted(cum_dist, cam["arc"], side="right") - 1,
                       0, len(cum_dist) - 2)
    stop_no = np.clip(np.searchsorted(stop_dist, cam["arc"], side="right") - 1,
                      0, n_stops)
    # frames since the counter last ticked -> drives the per-stop pop
    since = np.zeros(n_frames)
    last = -999
    for i in range(n_frames):
        if i > 0 and stop_no[i] != stop_no[i - 1]:
            last = i
        since[i] = i - last

    # seamless loop: dissolve the route/dots/HUD over the tail of the reveal so
    # the last frame converges on frame 0 (bare basemap + city labels) -- the
    # camera itself already loops exactly (reveal ends at the same wide shot
    # the hook starts from), so this is purely a content fade.
    fade_frames = int(round(C.LOOP_FADE_SEC * C.FPS))
    reveal_start = int(np.argmax(cam["phase"] == "reveal")) if n_frames else 0
    fade_start = max(n_frames - fade_frames, reveal_start)

    city_lon = np.array([c["lon"] for c in cities]) if cities else np.zeros(0)
    city_lat = np.array([c["lat"] for c in cities]) if cities else np.zeros(0)
    city_name = [c["name"] for c in cities]

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{C.OUT_W}x{C.OUT_H}", "-r", str(C.FPS), "-i", "-",
        "-an", "-c:v", "libx264", "-preset", C.PRESET, "-crf", str(C.CRF),
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
        "-movflags", "+faststart", out_path,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    t0 = time.time()
    bar = progress.bar(n_frames, unit=" frames")
    for i in range(n_frames):
        clon, clat, hw = cam["lon"][i], cam["lat"][i], cam["hw"][i]
        phase, pt, arc = cam["phase"][i], cam["ptime"][i], cam["arc"][i]
        box = vp.bounds(clon, clat, hw)
        canvas = vp.crop(box).astype(np.float32)

        lf = 0.0
        if C.LOOP_SEAMLESS and i >= fade_start:
            # denominator +1 keeps lf just under 1.0 on the final frame, so it
            # doesn't sit as a duplicate-looking still right before the wrap
            lf = gfx.ease_in_out((i - fade_start + 1) / (n_frames - fade_start + 1))
        pre = canvas.copy() if lf > 0 else None

        # zoom-independent scale factor for anything that should stay readable
        zf = float(np.clip(C.ZOOM_MAX_DEG / hw, 0.35, 1.6))

        sx, sy = vp.project(lons, lats, box)
        # stop-only screen coords, used for dots/rings which should land on
        # stores, not on the dense road-vertex geometry
        psx, psy = sx[stop_vert], sy[stop_vert]
        on_stops = (psx > -80) & (psx < C.OUT_W + 80) & (psy > -80) & (psy < C.OUT_H + 80)

        v = int(vert_idx[i])   # road-vertex segment the head is currently on
        k = int(stop_no[i])    # most recently visited store
        ignite = gfx.ease_out_cubic(pt / 0.55) if phase == "hook" else 1.0

        # ---- pending stops (dim), visited stops (lit)
        if phase != "hook":
            # dim preview of the whole loop so the shape reads before it fills in
            gfx.flat_polyline(canvas, np.c_[sx, sy], C.DOT_PENDING,
                              width=max(1, int(S(3) * zf)), alpha=0.85)

        pending = on_stops.copy()
        pending[:k + 1] = False
        if pending.any():
            gfx.flat_dots(canvas, np.c_[psx[pending], psy[pending]],
                          C.DOT_PENDING, radius=max(2, int(S(5) * zf)),
                          alpha=0.9 * ignite)

        if phase != "hook":
            # ---- travelled route
            seg = min(v + 1, len(sx) - 1)
            frac = 0.0
            if cum_dist[seg] > cum_dist[v]:
                frac = (arc - cum_dist[v]) / (cum_dist[seg] - cum_dist[v])
            hx = sx[v] + frac * (sx[seg] - sx[v])
            hy = sy[v] + frac * (sy[seg] - sy[v])

            path = np.c_[np.append(sx[:v + 1], hx), np.append(sy[:v + 1], hy)]
            gfx.neon_polyline(
                canvas, path, C.NEON_CORE, C.NEON_MID, C.NEON_OUTER,
                core_w=max(2, int(S(5) * zf)), mid_w=max(3, int(S(10) * zf)), intensity=0.85,
            )
            # hot tail is arc-length based (not vertex-count based), since a
            # road leg can be anywhere from a few vertices to a few hundred
            tail_cut = int(np.clip(np.searchsorted(cum_dist, arc - C.TAIL_KM), 0, len(path) - 1))
            tail = path[tail_cut:]
            gfx.neon_polyline(
                canvas, tail, C.NEON_CORE, C.NEON_CORE, C.NEON_MID,
                core_w=max(2, int(S(6) * zf)), mid_w=max(3, int(S(13) * zf)), intensity=0.6,
            )

            vis = on_stops.copy()
            vis[k + 1:] = False
            if vis.any():
                gfx.neon_dots(canvas, np.c_[psx[vis], psy[vis]], C.DOT_VISITED,
                              radius=max(2, int(S(7) * zf)), glow_sigma=S(16) * zf,
                              intensity=0.9)

            age = since[i] / (0.55 * C.FPS)
            if age < 1.0:
                gfx.ring(canvas, psx[k], psy[k], S(28) * zf + S(150) * zf * age,
                         C.NEON_MID, width=max(1, int(S(4) * zf)),
                         alpha=0.85 * (1 - age) ** 2)
            pop = float(np.exp(-since[i] / (0.16 * C.FPS)))
            gfx.head_flare(canvas, hx, hy, C.HEAD_COLOR,
                           size=max(24, int(S(118) * zf * (1 + 0.7 * pop))),
                           intensity=0.85 + 0.7 * pop)
        else:
            gfx.head_flare(canvas, sx[0], sy[0], C.ACCENT, size=max(24, int(S(170))),
                           intensity=0.5 * ignite)

        if pre is not None:
            # dissolve everything drawn above back to the bare basemap crop,
            # which is what frame 0 already is -- makes the loop seamless
            canvas *= (1 - lf)
            canvas += pre * lf

        # ---- city labels at constant screen size
        if len(city_lon):
            keep = 6 if hw > 4 else 14
            cx, cy = vp.project(city_lon[:keep], city_lat[:keep], box)
            vis_c = ((cx > -40) & (cx < C.OUT_W + 40) &
                     (cy > S(240)) & (cy < C.OUT_H - S(C.SAFE_BOTTOM)))
            if vis_c.any():
                gfx.flat_dots(canvas, np.c_[cx[vis_c], cy[vis_c]], C.TEXT_DIM,
                              max(2, int(S(4))), 0.55)
                for j in np.flatnonzero(vis_c):
                    gfx.draw_text(canvas, city_name[j].upper(), S(30),
                                  (cx[j] + S(16), cy[j]), C.TEXT_DIM, anchor="lm",
                                  alpha=0.6, letter_spacing=S(2))

        canvas *= vignette

        _hud(canvas, phase, pt, arc, k, n_stops, total_km, conv, unit, total_hours,
            fade=1.0 - lf)

        proc.stdin.write(np.clip(canvas, 0, 255).astype(np.uint8).tobytes())
        bar.update(i + 1)

    proc.stdin.close()
    proc.wait()
    progress.done(f"wrote {out_path} ({time.time() - t0:.0f}s)")


def _hud(canvas, phase, pt, arc, k, n_stops, total_km, conv, unit, total_hours=None,
        fade=1.0):
    """fade: 1.0 normally; ramps to 0.0 during the loop-seamless dissolve at
    the tail of the reveal (see LOOP_SEAMLESS in render()). Named `loop_fade`
    inside the reveal branch below since `fade` is already used locally for
    the whip fade-in."""
    loop_fade = fade
    CX = C.OUT_W // 2
    TW = C.OUT_W - 2 * S(C.TEXT_MARGIN)   # usable headline width

    if phase == "hook":
        for li, line in enumerate(C.HOOK_LINES):
            t = gfx.clamp01((pt - 0.06 * li) / 0.30)
            if t <= 0:
                continue
            s = 0.82 + 0.18 * gfx.ease_out_back(t)
            gfx.draw_text(canvas, line, S(118) * s, (CX, S(300 + li * 132)),
                          C.TEXT, C.NEON_MID, glow_sigma=S(30), glow_gain=0.9,
                          alpha=gfx.ease_out_cubic(t), letter_spacing=S(2),
                          max_width=TW)
        t = gfx.clamp01((pt - 0.30) / 0.30)
        gfx.draw_text(canvas, C.HOOK_SUB.upper(), S(46), (CX, S(300 + len(C.HOOK_LINES) * 132)),
                      C.TEXT_DIM, C.ACCENT, glow_sigma=S(22), glow_gain=0.5,
                      alpha=gfx.ease_out_cubic(t), letter_spacing=S(7),
                      max_width=TW)
        t = gfx.clamp01((pt - 0.45) / 0.35)
        gfx.panel(canvas, CX - S(340), S(1120), S(680), S(300),
                  alpha=0.55 * gfx.clamp01((pt - 0.45) / 0.35))
        gfx.draw_odometer(canvas, fmt_int(n_stops), S(168), (CX, S(1230)),
                          C.TEXT, C.NEON_MID, alpha=gfx.ease_out_cubic(t),
                          max_width=S(640))
        gfx.draw_text(canvas, "STOPS", S(44), (CX, S(1360)), C.TEXT_DIM,
                      alpha=gfx.ease_out_cubic(t), letter_spacing=S(12))
        return

    fade = gfx.clamp01(pt / 0.25) if phase == "whip" else 1.0

    if phase in ("whip", "drive"):
        # SAFE_TOP clears TikTok's top nav bar; the text's own glow bleeds
        # upward past its anchor, so add margin beyond the boundary itself
        gfx.draw_text(canvas, " ".join(C.HOOK_LINES), S(40), (CX, S(C.SAFE_TOP) + S(90)),
                      C.TEXT_DIM, C.NEON_MID, glow_sigma=S(18), glow_gain=0.35,
                      alpha=0.85 * fade, letter_spacing=S(6), max_width=TW)

        # shifted down from the panel's original y=1050 so it sits just above
        # the real username/caption line instead of blocking the route --
        # SAFE_BOTTOM's 430px reservation is conservative; the actual TikTok
        # caption text only occupies the bottom ~120-160px of that zone.
        gfx.panel(canvas, CX - S(400), S(1210), S(800), S(410), alpha=0.5 * fade)
        gfx.draw_text(canvas, f"STOP {k + 1} / {n_stops}", S(44), (CX, S(1260)),
                      C.TEXT, C.ACCENT, glow_sigma=S(20), glow_gain=0.55,
                      alpha=fade, letter_spacing=S(4))

        # two co-equal columns: mileage and gas cost, ticking up together.
        # cost is always priced off true miles, independent of USE_MILES.
        gas_cost = (arc / KM_PER_MI) * C.GAS_COST_PER_MILE
        gfx.draw_odometer(canvas, fmt_int(arc * conv), S(108), (CX - S(210), S(1410)),
                          C.TEXT, C.NEON_MID, alpha=fade, max_width=S(340))
        gfx.draw_text(canvas, f"{unit} DRIVEN", S(32), (CX - S(210), S(1505)),
                      C.TEXT_DIM, alpha=0.9 * fade, letter_spacing=S(6))
        gfx.draw_odometer(canvas, fmt_money(gas_cost), S(108), (CX + S(210), S(1410)),
                          C.TEXT, C.NEON_MID, alpha=fade, max_width=S(340))
        gfx.draw_text(canvas, "GAS SO FAR", S(32), (CX + S(210), S(1505)),
                      C.TEXT_DIM, alpha=0.9 * fade, letter_spacing=S(6))
        gfx.progress_bar(canvas, CX - S(350), S(1602), S(700), S(10),
                         arc / max(total_km, 1e-9), C.NEON_MID, C.NEON_OUTER,
                         alpha=fade)
        return

    # ---- reveal
    t = gfx.ease_out_cubic(pt / 0.45) * loop_fade
    gfx.panel(canvas, CX - S(430), S(1010), S(860), S(460), alpha=0.62 * t)
    gfx.draw_text(canvas, C.REVEAL_LINE, S(104), (CX, S(300)), C.TEXT, C.NEON_MID,
                  glow_sigma=S(32), glow_gain=0.95, alpha=t, letter_spacing=S(3),
                  max_width=TW)

    if total_hours is not None:
        # real summed OSRM leg duration, when road routing is available
        hours = total_hours
    else:
        # fallback: constant-speed estimate against whatever distance we have
        hours = (total_km * conv) / (C.AVG_SPEED_MPH if C.USE_MILES else C.AVG_SPEED_MPH * KM_PER_MI)
    gas_cost_total = (total_km / KM_PER_MI) * C.GAS_COST_PER_MILE
    rows = [
        (fmt_int(total_km * conv), f"{unit} TOTAL"),
        (fmt_int(n_stops), "STOPS HIT"),
        (fmt_int(hours), "HOURS DRIVING"),
        (fmt_money(gas_cost_total), "IN GAS"),
    ]
    for ri, (val, lab) in enumerate(rows):
        tt = gfx.ease_out_cubic(gfx.clamp01((pt - 0.18 - 0.09 * ri) / 0.24)) * loop_fade
        y = S(1090 + ri * 112)
        gfx.draw_odometer(canvas, val, S(82), (CX - S(30), y + S(22) * (1 - tt)),
                          C.TEXT, C.NEON_MID, anchor="rm", alpha=tt)
        gfx.draw_text(canvas, lab, S(30), (CX + S(10), y + S(22) * (1 - tt)),
                      C.TEXT_DIM, anchor="lm", alpha=0.9 * tt, letter_spacing=S(4))
