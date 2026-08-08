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

    def _warp_level(self, lvl, px0, px1, py0, py1):
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

    def crop(self, cam):
        W, E, S, N = self.extent
        lon0, lon1, lat0, lat1 = cam
        sx = self.bw / (E - W)
        sy = self.bh / (N - S)
        px0, px1 = (lon0 - W) * sx, (lon1 - W) * sx
        py0, py1 = (N - lat1) * sy, (N - lat0) * sy

        # pick the level from whichever axis needs more detail -- the vertical
        # extent is scaled by cos(lat) (see bounds()), so at lower latitudes
        # the vertical resample factor can run up to ~1.24x the horizontal
        # one; picking off fx alone would undersample that axis
        fx = (px1 - px0) / C.OUT_W
        fy = (py1 - py0) / C.OUT_H
        factor = max(fx, fy, 1e-6)
        log_lvl = np.clip(np.log2(factor), 0, len(self.pyr) - 1)
        lvl = int(np.floor(log_lvl))
        frac = log_lvl - lvl

        # trilinear: blend the two adjacent mip levels instead of hard-
        # switching at each octave crossing. A plain round() snap means the
        # background's detail visibly pops 3 times during the whip and 3
        # times during the reveal as factor sweeps across the pyramid.
        out = self._warp_level(lvl, px0, px1, py0, py1)
        if frac > 1e-3 and lvl + 1 < len(self.pyr):
            out2 = self._warp_level(lvl + 1, px0, px1, py0, py1)
            out = cv2.addWeighted(out, 1 - frac, out2, frac, 0)
        return out


def fmt_int(v):
    return f"{int(round(v)):,}"


def fmt_money(v):
    return f"${int(round(v)):,}"


def render(lons, lats, cum_dist, stop_vert, stop_dist, total_hours,
          cam, basemap_img, meta, cities, out_path,
          cover_path=None, cover_only=False):
    """lons/lats/cum_dist are per road-vertex (dense). stop_vert[s] is the
    vertex index of stop s; stop_dist is cum_dist[stop_vert]. When routing is
    disabled every vertex is a stop, so stop_vert == arange(n) and the two
    index spaces collapse back to the old behaviour."""
    global _S
    _S = C.OUT_W / 1080.0
    gfx.set_scale(_S)
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
    # every stop hit fires its own ring, independent of and overlapping with
    # any other still-live ring -- not rate-limited, just each with its own
    # STOP_RING_SEC lifetime. `stop_no` can advance by more than 1 in a
    # single frame on dense datasets (multiple stops within one frame's arc
    # step), so each stop between the previous and current index gets its
    # own event rather than only the latest one firing.
    ring_len = C.STOP_RING_SEC * C.FPS
    accent_events = []   # (frame, stop_idx), frame non-decreasing
    prev = int(stop_no[0])
    for i in range(1, n_frames):
        cur = int(stop_no[i])
        if cur > prev:
            accent_events.extend((i, s) for s in range(prev + 1, cur + 1))
            prev = cur
    accent_frames = np.array([e[0] for e in accent_events], dtype=np.int64)
    accent_stops = np.array([e[1] for e in accent_events], dtype=np.int64)

    # seamless loop: the camera already loops exactly (reveal ends at the same
    # wide shot the hook starts from), and the map content now matches too --
    # by the end of the reveal every stop is visited and the full loop is lit,
    # which is exactly frame 0's state. So there's nothing left to dissolve in
    # the map layer; only the HUD text differs across the seam (reveal's stats
    # block vs. the hook's headline), and that's crossfaded in _hud() instead.
    fade_frames = int(round(C.LOOP_FADE_SEC * C.FPS))
    reveal_start = int(np.argmax(cam["phase"] == "reveal")) if n_frames else 0
    fade_start = max(n_frames - fade_frames, reveal_start)

    # last frame of the hook: end of the hold beat, with the full loop lit and
    # the question copy fully settled -- a stronger cover than a mid-fade-in
    # frame would be, and it's the frame right before the un-draw starts.
    hook = cam["phase"] == "hook"
    cover_idx = int(np.flatnonzero(hook)[-1]) if hook.any() else 0

    city_lon = np.array([c["lon"] for c in cities]) if cities else np.zeros(0)
    city_lat = np.array([c["lat"] for c in cities]) if cities else np.zeros(0)
    city_name = [c["name"] for c in cities]
    city_pop = np.array([c["pop"] for c in cities], float) if cities else np.zeros(0)
    log_pop = np.log10(np.maximum(city_pop, 1.0))

    # the label population threshold slides log-linearly between the tightest
    # drive zoom and the wide establishing shot, so towns fade in as the
    # camera pushes in and drop off again on the wide shots
    zoom_span = np.log(cam["wide_hw"] / C.ZOOM_MIN_DEG)
    log_pop_tight = np.log10(C.CITY_LABEL_POP_TIGHT)
    log_pop_wide = np.log10(C.CITY_LABEL_POP_WIDE)
    # label size ramps across the same log-population range as the pool itself
    size_lo, size_hi = np.log10(C.CITY_MIN_POP), np.log10(5e6)

    # HUD layout constants (see _hud/_hook_copy below) -- also needed here so
    # city labels can avoid drawing under the headline/stat block
    CX = C.OUT_W // 2
    TW = C.OUT_W - 2 * S(C.TEXT_MARGIN)

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{C.OUT_W}x{C.OUT_H}", "-r", str(C.FPS), "-i", "-",
        "-an", "-c:v", "libx264", "-preset", C.PRESET, "-crf", str(C.CRF),
        # no explicit -level: 4.1 caps out at 1080p-class streams and would make
        # 1440p/2160p output non-conformant; x264 picks the right level itself.
        # aq-mode 3 biases bits toward dark, low-contrast regions, which is
        # exactly the near-black background and smooth glow gradients here --
        # the content most prone to banding in a re-encode.
        "-pix_fmt", "yuv420p", "-profile:v", "high",
        "-x264-params", "aq-mode=3:aq-strength=1.0",
        # tag the stream as bt709 explicitly -- without this, swscale's
        # RGB->YUV default and the player's assumption can disagree (601 vs
        # 709), shifting the saturated cyan/magenta this whole look depends
        # on, and an untagged limited-range conversion crushes the near-black
        # BG toward the floor. Cheap, and survives TikTok's re-encode.
        "-vf", "scale=out_color_matrix=bt709:out_range=tv",
        "-colorspace", "bt709", "-color_primaries", "bt709",
        "-color_trc", "bt709", "-color_range", "tv",
        "-movflags", "+faststart", out_path,
    ]
    proc = None
    if not cover_only:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    frames = [cover_idx] if cover_only else range(n_frames)
    t0 = time.time()
    bar = progress.bar(len(frames), unit=" frames")
    for n_done, i in enumerate(frames, 1):
        clon, clat, hw = cam["lon"][i], cam["lat"][i], cam["hw"][i]
        phase, pt, arc = cam["phase"][i], cam["ptime"][i], cam["arc"][i]
        box = vp.bounds(clon, clat, hw)
        canvas = vp.crop(box).astype(np.float32)

        lf = 0.0
        if C.LOOP_SEAMLESS and i >= fade_start:
            # HUD-only crossfade now (see the loop-seamless note above) --
            # denominator +1 keeps lf just under 1.0 on the final frame, so it
            # doesn't sit as a duplicate-looking still right before the wrap
            lf = gfx.ease_in_out((i - fade_start + 1) / (n_frames - fade_start + 1))

        # zoom-independent scale factor for anything that should stay readable
        zf = float(np.clip(C.ZOOM_MAX_DEG / hw, 0.35, 1.6))

        sx, sy = vp.project(lons, lats, box)
        # stop-only screen coords, used for dots/rings which should land on
        # stores, not on the dense road-vertex geometry
        psx, psy = sx[stop_vert], sy[stop_vert]
        on_stops = (psx > -80) & (psx < C.OUT_W + 80) & (psy > -80) & (psy < C.OUT_H + 80)

        v = int(vert_idx[i])   # road-vertex segment the head is currently on
        k = int(stop_no[i])    # most recently visited store

        # ---- hook/whip breathing state, needed below to fade the drive-only
        # dim/pending layers in across hook->whip (see the loop-seam note)
        if phase == "hook":
            hook_a = 1.0
            breath = np.sin(2 * np.pi * C.HOOK_PULSE_CYCLES * pt)
        elif phase == "whip":
            hook_a = 1.0 - gfx.clamp01(pt / 0.40)
            breath = 0.0
        else:
            hook_a, breath = 0.0, 0.0

        # ---- pending stops (dim), visited stops (lit)
        # dim preview of the whole loop so the shape reads before it fills in.
        # Faded by (1-hook_a) through hook/whip: frame 0 has k=0 (all but one
        # stop pending) while the reveal's last frame has none pending, so
        # without this fade the dot field would jump red at the loop wrap --
        # this ramp and the hook overlay's own fade converge on the same
        # (fully-drawn, nothing-pending-looking) state from both sides.
        gfx.flat_polyline(canvas, np.c_[sx, sy], C.DOT_PENDING,
                          width=max(1.0, S(3) * zf) * (1 - hook_a), alpha=0.85)

        pending = on_stops.copy()
        pending[:k + 1] = False
        if pending.any():
            gfx.flat_dots(canvas, np.c_[psx[pending], psy[pending]],
                          C.DOT_PENDING, radius=max(1.0, S(5) * zf),
                          alpha=0.9 * (1 - hook_a))

        # loop-seamless dissolve (lf, see fade_start above): the reveal's own
        # `pending` set is empty by now (k=n_stops), but frame 0 has every
        # store but the first still pending -- draw that set in as lf -> 1 so
        # the dot field is already in frame 0's state by the time it wraps,
        # instead of the whole field jumping red on the cut
        if lf > 0.003:
            pending_seam = on_stops.copy()
            pending_seam[:1] = False
            if pending_seam.any():
                gfx.flat_dots(canvas, np.c_[psx[pending_seam], psy[pending_seam]],
                              C.DOT_PENDING, radius=max(1.0, S(5) * zf), alpha=0.9 * lf)

        # ---- travelled route (arc/v/k are all 0 through hook+whip, so this
        # draws almost nothing there -- just a still flare at the start pin,
        # which is exactly the state the hook/whip overlay below fades into)
        seg = min(v + 1, len(sx) - 1)
        frac = 0.0
        if cum_dist[seg] > cum_dist[v]:
            frac = (arc - cum_dist[v]) / (cum_dist[seg] - cum_dist[v])
        hx = sx[v] + frac * (sx[seg] - sx[v])
        hy = sy[v] + frac * (sy[seg] - sy[v])

        path = np.c_[np.append(sx[:v + 1], hx), np.append(sy[:v + 1], hy)]
        gfx.neon_polyline(
            canvas, path, C.NEON_CORE, C.NEON_MID, C.NEON_OUTER,
            core_w=max(1.0, S(5) * zf), mid_w=max(1.0, S(10) * zf), intensity=0.85,
        )
        # hot tail is arc-length based (not vertex-count based), since a
        # road leg can be anywhere from a few vertices to a few hundred.
        # Tapered in 4 arc-length chunks instead of one hard-edged block: a
        # uniform intensity=0.6 on top of the base 0.85 clips to solid white
        # with a visible boundary scrolling along the route, whereas this
        # ramps 0 -> peak so the tail reads as a comet, not a step. Also
        # faded by (1-lf) across the loop-seamless dissolve, since the last
        # reveal frame has 35km of tail lit and frame 0 has none.
        tail_peak = 0.55 * (1 - lf)
        if tail_peak > 0.003:
            arc0 = arc - C.TAIL_KM
            n_chunks = 4
            for ci in range(n_chunks):
                a0 = arc0 + (arc - arc0) * ci / n_chunks
                a1 = arc0 + (arc - arc0) * (ci + 1) / n_chunks
                i0 = int(np.clip(np.searchsorted(cum_dist, a0), 0, len(path) - 1))
                i1 = int(np.clip(np.searchsorted(cum_dist, a1), i0, len(path) - 1))
                if i1 <= i0:
                    continue
                gfx.neon_polyline(
                    canvas, path[i0:i1 + 1], C.NEON_CORE, C.NEON_CORE, C.NEON_MID,
                    core_w=max(1.0, S(6) * zf), mid_w=max(1.0, S(13) * zf),
                    intensity=tail_peak * (ci + 1) / n_chunks,
                )

        vis = on_stops.copy()
        vis[k + 1:] = False
        if vis.any():
            gfx.neon_dots(canvas, np.c_[psx[vis], psy[vis]], C.DOT_VISITED,
                          radius=max(1.0, S(7) * zf), glow_sigma=S(16) * zf,
                          intensity=0.9)

        # every stop hit gets its own ring for its own lifetime, independent
        # of and overlapping with any other still-live ring -- not just the
        # newest, and never rate-limited, so nothing is cut off or skipped
        lo = np.searchsorted(accent_frames, i - ring_len, side="left")
        hi = np.searchsorted(accent_frames, i, side="right")
        for e, es in zip(accent_frames[lo:hi], accent_stops[lo:hi]):
            age = (i - e) / ring_len
            if age < 1.0:
                es = int(es)
                gfx.ring(canvas, psx[es], psy[es], S(28) * zf + S(150) * zf * age,
                         C.NEON_MID, width=S(4) * zf, alpha=0.85 * (1 - age) ** 2)
        # plain, constant head marker -- the pulse lives entirely in the
        # rings above, not in the flare's own size/brightness
        gfx.head_flare(canvas, hx, hy, C.HEAD_COLOR,
                       size=max(24, int(S(118) * zf)), intensity=0.9)

        # ---- hook/whip overlay: the loop is already fully drawn (it has to
        # match the reveal's end state -- see the loop-seamless notes above),
        # so the opening can't draw itself on. Instead the whole lit loop
        # breathes -- brightens and dims together -- for the length of the
        # hook, then the whole overlay fades out across the first 40% of the
        # whip so DRIVE's normal per-frame state (nothing travelled yet, all
        # pending) is what's left underneath.
        #
        # `breath` is a sine that starts AND ends at 0 across the hook (see
        # HOOK_PULSE_CYCLES in config.py), so frame 0 and the hook's last
        # frame both sit at the same resting brightness as the reveal's end
        # state -- the breathing never pops the seam or the whip handoff.
        # (hook_a/breath were computed above, before the pending-dot fades.)
        if hook_a > 0.003:
            core_m = 1.0 + C.HOOK_PULSE_GAIN * max(breath, 0.0) \
                         + C.HOOK_PULSE_DIP * min(breath, 0.0)
            glow_m = 1.0 + C.HOOK_PULSE_BLOOM * max(breath, 0.0)

            gfx.neon_polyline(
                canvas, np.c_[sx, sy], C.NEON_CORE, C.NEON_MID, C.NEON_OUTER,
                core_w=max(1.0, S(5) * zf), mid_w=max(1.0, S(10) * zf),
                intensity=0.85 * hook_a * core_m, glow_boost=glow_m,
            )
            if on_stops.any():
                gfx.neon_dots(canvas, np.c_[psx[on_stops], psy[on_stops]], C.DOT_VISITED,
                              radius=max(1.0, S(7) * zf),
                              glow_sigma=S(16) * zf * (1 + 0.5 * max(breath, 0.0)),
                              intensity=0.9 * hook_a * core_m)

        # ---- city labels: the population threshold slides with zoom, so
        # small towns fade in as the camera tightens and drop off the wide
        # shots instead of the old hard 6/14 rank-based snap
        if len(city_lon):
            u = gfx.clamp01(np.log(hw / C.ZOOM_MIN_DEG) / zoom_span)
            thresh = log_pop_tight + u * (log_pop_wide - log_pop_tight)
            a_city = np.clip((log_pop - thresh) / C.CITY_LABEL_FADE_DECADES, 0, 1)
            cand = np.flatnonzero(a_city > 0.02)   # already biggest-first

            if len(cand):
                cx, cy = vp.project(city_lon[cand], city_lat[cand], box)
                # feathered on-screen mask: a hard boolean cutoff makes a label
                # blink out the instant it crosses the frame edge or a safe-zone
                # line; ramping alpha over a ~60px band removes that pop
                feather = S(60)
                ex = np.minimum(np.clip((cx - (-40)) / feather, 0, 1),
                                np.clip(((C.OUT_W + 40) - cx) / feather, 0, 1))
                ey = np.minimum(np.clip((cy - S(240)) / feather, 0, 1),
                                np.clip(((C.OUT_H - S(C.SAFE_BOTTOM)) - cy) / feather, 0, 1))
                edge_a = ex * ey
                on = edge_a > 0.02

                # pre-seed collision with the HUD's own text/panel footprint
                # (+ the like/comment/share rail) so labels stop drawing
                # under the headline/stat block instead of just each other
                placed = _hud_rects(phase, CX, TW)

                shown = 0
                for m in np.flatnonzero(on):
                    j = cand[m]
                    t_sz = gfx.clamp01((log_pop[j] - size_lo) / (size_hi - size_lo))
                    size = S(C.CITY_LABEL_SIZE_MIN +
                             t_sz * (C.CITY_LABEL_SIZE_MAX - C.CITY_LABEL_SIZE_MIN))
                    label = city_name[j].upper()
                    # measure() returns ink WIDTH only; caps-only labels are
                    # about `size` tall, close enough for collision boxes
                    w = gfx.measure(label, gfx.font(size), S(2))
                    bx, by = cx[m] + S(16), cy[m] - size / 2

                    # continuous overlap fraction instead of a binary
                    # accept/reject -- two boxes grazing in and out of
                    # contact as the camera pans faded rather than strobed
                    overlap = 0.0
                    for px, py, pw, ph in placed:
                        ix = max(0.0, min(bx + w, px + pw) - max(bx, px))
                        iy = max(0.0, min(by + size, py + ph) - max(by, py))
                        if ix > 0 and iy > 0:
                            overlap = max(overlap, (ix * iy) / max(w * size, 1e-6))
                    coll_a = gfx.clamp01(1 - overlap * 2.5)
                    if coll_a <= 0.03:
                        continue               # a bigger city/the HUD owns this spot

                    # ramp the last few slots out instead of a hard MAX break,
                    # so the lowest-ranked visible label doesn't pop in/out as
                    # a bigger city scrolls through and claims its slot
                    slot_a = gfx.clamp01((C.CITY_LABEL_MAX - shown) / 3.0)
                    if slot_a <= 0.03:
                        break

                    a = a_city[j] * edge_a[m] * coll_a * slot_a
                    if a <= 0.02:
                        continue
                    shown += 1
                    placed.append((bx, by, w, size))
                    gfx.flat_dots(canvas, np.array([[cx[m], cy[m]]]), C.TEXT_DIM,
                                  max(2, int(S(4))), 0.55 * a)
                    gfx.draw_text(canvas, label, size, (cx[m] + S(16), cy[m]),
                                  C.TEXT_DIM, anchor="lm", alpha=0.6 * a,
                                  letter_spacing=S(2))

        canvas *= vignette

        # eases the whole drive HUD out over the last half-second before the
        # reveal, instead of it vanishing on the same frame the camera's
        # follow speed hits 0 at the drive/reveal seam (see _hud's drive_out)
        drive_out = gfx.clamp01((reveal_start - i) / (0.5 * C.FPS))

        _hud(canvas, phase, pt, arc, k, n_stops, total_km, conv, unit, total_hours,
            fade=1.0 - lf, drive_out=drive_out)

        # cover PNG is a single still, not a video frame heading into a lossy
        # re-encode -- write it from the clean (pre-dither) canvas so it
        # doesn't carry the per-pixel noise the video dither intentionally adds
        if cover_path and i == cover_idx:
            cover_u8 = np.clip(canvas, 0, 255).astype(np.uint8)
            cv2.imwrite(cover_path, cv2.cvtColor(cover_u8, cv2.COLOR_RGB2BGR))

        # ±0.5 LSB dither before the one-and-only quantization step: breaks up
        # 8-bit banding in the vignette/glow before the encoder ever sees it,
        # which matters more once TikTok's own re-encode compounds it further
        canvas += np.random.uniform(-0.5, 0.5, canvas.shape).astype(np.float32)
        out_u8 = np.clip(canvas, 0, 255).astype(np.uint8)
        if proc is not None:
            proc.stdin.write(out_u8.tobytes())
        bar.update(n_done)

    if proc is not None:
        proc.stdin.close()
        proc.wait()
        progress.done(f"wrote {out_path} ({time.time() - t0:.0f}s)")
    if cover_path:
        progress.done(f"wrote {cover_path}")


def _hud_rects(phase, CX, TW):
    """Approximate bounding boxes of whatever HUD text/panels are on screen
    for `phase`, in the same (x, y, w, h) shape as the city-label collision
    boxes. Used to seed that collision list so labels stop drawing under the
    headline/stat block instead of only avoiding each other, plus the
    like/comment/share rail, which nothing previously avoided."""
    rects = [(C.OUT_W - S(C.SAFE_RIGHT), 0.0, S(C.SAFE_RIGHT), float(C.OUT_H))]
    if phase == "hook":
        rects += [
            (CX - TW / 2, S(220), TW, S(300)),          # headline (2 lines) + sub
            (CX - S(340), S(1120), S(680), S(300)),      # STOPS panel
        ]
    elif phase in ("whip", "drive"):
        rects += [
            (CX - TW / 2, S(C.SAFE_TOP), TW, S(140)),    # small header
            (CX - S(400), S(1210), S(800), S(410)),      # drive stat panel
        ]
    else:  # reveal
        rects += [
            (CX - TW / 2, S(220), TW, S(160)),           # "THE FULL LOOP"
            (CX - S(430), S(1010), S(860), S(460)),      # stats panel
        ]
    return rects


def _hook_copy(canvas, CX, TW, n_stops, pt, alpha=1.0, draw_panel=True):
    """Headline + open-loop question + stop count. `pt` drives a ~4-frame
    entrance ease (called with pt=1.0 for an already-settled draw); `alpha`
    is a separate overall multiplier, used by the reveal's tail crossfade so
    this copy is already at full opacity by the time the loop wraps.
    `draw_panel=False` skips this call's own panel plate -- used during that
    same crossfade, where the reveal's own (larger) panel already covers the
    same area and drawing both would double-darken the overlap.

    The ramp windows below start before pt=0 (the +0.09 offset) so frame 0 --
    the very first frame of the whole clip, where there's no prior frame to
    pop from -- reads as already-lit rather than blank; what little is left
    settles over the next couple of frames instead of a hard pop.
    """
    pt = pt + 0.09
    for li, line in enumerate(C.HOOK_LINES):
        t = gfx.clamp01((pt - 0.008 * li) / 0.05)
        if t <= 0:
            continue
        s = 0.82 + 0.18 * gfx.ease_out_back(t)
        gfx.draw_text(canvas, line, S(118) * s, (CX, S(300 + li * 132)),
                      C.TEXT, C.NEON_MID, glow_sigma=S(30), glow_gain=0.9,
                      alpha=gfx.ease_out_cubic(t) * alpha, letter_spacing=S(2),
                      max_width=TW)
    t = gfx.clamp01((pt - 0.02) / 0.05)
    gfx.draw_text(canvas, C.HOOK_SUB.upper(), S(46), (CX, S(300 + len(C.HOOK_LINES) * 132)),
                  C.TEXT_DIM, C.ACCENT, glow_sigma=S(22), glow_gain=0.5,
                  alpha=gfx.ease_out_cubic(t) * alpha, letter_spacing=S(7),
                  max_width=TW)
    t = gfx.clamp01((pt - 0.03) / 0.08)
    if draw_panel:
        gfx.panel(canvas, CX - S(340), S(1120), S(680), S(300), alpha=0.55 * t * alpha)
    gfx.draw_odometer(canvas, fmt_int(n_stops), S(168), (CX, S(1230)),
                      C.TEXT, C.NEON_MID, alpha=gfx.ease_out_cubic(t) * alpha,
                      max_width=S(640))
    gfx.draw_text(canvas, "STOPS", S(44), (CX, S(1360)), C.TEXT_DIM,
                  alpha=gfx.ease_out_cubic(t) * alpha, letter_spacing=S(12))


def _hud(canvas, phase, pt, arc, k, n_stops, total_km, conv, unit, total_hours=None,
        fade=1.0, drive_out=1.0):
    """fade: 1.0 normally; ramps to 0.0 during the loop-seamless dissolve at
    the tail of the reveal (see LOOP_SEAMLESS in render()). Named `loop_fade`
    inside the reveal branch below since `fade` is already used locally for
    the whip fade-in. drive_out: ramps 1.0 -> 0.0 over the last ~0.5s of the
    drive (see render()), so the whole bottom HUD eases out instead of
    vanishing on the same frame the camera's follow speed drops to 0 at the
    drive/reveal seam -- previously the single roughest cut in the video."""
    loop_fade = fade
    CX = C.OUT_W // 2
    TW = C.OUT_W - 2 * S(C.TEXT_MARGIN)   # usable headline width

    if phase == "hook":
        # near-instant now (was a multi-second slam-in) -- the whole point of
        # the cold open is that frame 0 already reads, so every ramp here is
        # just enough ease to avoid a hard 1-frame pop, not a build-up
        _hook_copy(canvas, CX, TW, n_stops, pt)
        return

    if phase == "whip":
        # mirrors the map layer's own hook_a fade (render()): the big hook
        # copy clears across the first 40% of the whip...
        hook_out = 1.0 - gfx.clamp01(pt / 0.40)
        if hook_out > 0.003:
            _hook_copy(canvas, CX, TW, n_stops, pt=1.0, alpha=hook_out)
        # ...and the small persistent header waits for that to mostly clear
        # before easing in, instead of both being on screen and mushing
        # together for the first third of the whip
        fade = gfx.clamp01((pt - 0.35) / 0.30)
    else:
        fade = 1.0
    fade *= drive_out

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
    # the stats block fades out over the first half of the loop-seamless
    # crossfade and the hook copy fades in over the second half, instead of
    # both ramping across the same window -- two different strings sharing
    # the same baseline (REVEAL_LINE / HOOK_LINES[0] both sit at y=300) read
    # as mush if they cross-dissolve simultaneously. `wrap` is progress
    # through that crossfade: 0 for all of the normal reveal, ramping to 1
    # only at the very end (loop_fade == 1 - wrap, see render()'s `lf`).
    wrap = 1.0 - loop_fade
    stats_out = gfx.clamp01(1 - wrap / 0.5)
    hook_in = gfx.clamp01((wrap - 0.45) / 0.55)

    t = gfx.ease_out_cubic(pt / 0.45) * loop_fade * stats_out
    reveal_panel_a = 0.62 * gfx.ease_out_cubic(pt / 0.45) * loop_fade * stats_out
    # the hook copy's own panel alpha, had it drawn one at this alpha/pt --
    # computed here (without drawing) just to merge into a single panel call
    hook_panel_a = 0.55 * gfx.clamp01((1.09 - 0.03) / 0.08) * hook_in
    gfx.panel(canvas, CX - S(430), S(1010), S(860), S(460),
             alpha=max(reveal_panel_a, hook_panel_a))
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
        tt = gfx.ease_out_cubic(gfx.clamp01((pt - 0.18 - 0.09 * ri) / 0.24)) * loop_fade * stats_out
        y = S(1090 + ri * 112)
        gfx.draw_odometer(canvas, val, S(82), (CX - S(30), y + S(22) * (1 - tt)),
                          C.TEXT, C.NEON_MID, anchor="rm", alpha=tt)
        gfx.draw_text(canvas, lab, S(30), (CX + S(10), y + S(22) * (1 - tt)),
                      C.TEXT_DIM, anchor="lm", alpha=0.9 * tt, letter_spacing=S(4))

    # crossfade the hook's copy IN as the stats fade out, so by the time the
    # camera wraps to frame 0 the hook copy is already at full opacity -- the
    # only thing that would otherwise pop across the seam, now that the map
    # content already matches (see the loop-seamless note in render()). Its
    # own panel is skipped (draw_panel=False): the merged panel above already
    # covers this same area at the right alpha.
    _hook_copy(canvas, CX, TW, n_stops, pt=1.0, alpha=hook_in, draw_panel=False)
