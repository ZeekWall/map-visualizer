"""Precomputes the whole camera path before a single frame is drawn.

Four acts: HOOK (wide, dots ignite) -> WHIP (fast zoom to the start pin) ->
DRIVE (follow cam) -> REVEAL (pull back to the finished loop).

Zoom during DRIVE is speed-adaptive: long empty highway legs pull the camera out
so they feel fast, dense metro clusters pull it in so the stops read individually.
Everything is smoothed in log-zoom space, which is what stops a zoom from feeling
like it snaps.
"""

import numpy as np

import config as C


def _gauss_smooth(a, sigma_frames):
    if sigma_frames < 0.5 or len(a) < 3:
        return a
    r = int(max(1, round(sigma_frames * 3)))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma_frames) ** 2)
    k /= k.sum()
    pad = np.pad(a, (r, r), mode="edge")
    return np.convolve(pad, k, mode="valid")


def wide_half_width(extent, out_w, out_h):
    """Half-width in degrees that fits the whole region in a 9:16 frame."""
    W, E, S, N = extent
    clat = (S + N) / 2
    need_w = (E - W) / 2
    # half_h = half_w * (out_h/out_w) * cos(lat)  ->  invert for the vertical fit
    need_from_h = ((N - S) / 2) / ((out_h / out_w) * np.cos(np.radians(clat)))
    return max(need_w, need_from_h) * 1.12


def build(lons, lats, cum_dist, extent, stop_dist=None):
    """lons/lats/cum_dist are per road-vertex (dense). stop_dist is the
    per-stop cumulative distance (cum_dist[stop_vert]) used to blend DRIVE
    pacing between constant-speed and constant-stops; defaults to cum_dist
    itself when every vertex is a stop (routing disabled)."""
    if stop_dist is None:
        stop_dist = cum_dist

    total_frames = int(C.FPS * C.DURATION_SEC)
    n_hook = int(total_frames * C.ACT_HOOK)
    n_whip = int(total_frames * C.ACT_WHIP)
    n_reveal = int(total_frames * C.ACT_REVEAL)
    n_drive = total_frames - n_hook - n_whip - n_reveal

    total = cum_dist[-1]
    n_stops = len(stop_dist) - 1

    # ---- DRIVE: arc position blended between constant-speed and constant-stops
    u = np.linspace(0.0, 1.0, n_drive)
    d_by_dist = u * total
    d_by_stop = np.interp(u * n_stops, np.arange(n_stops + 1), stop_dist)
    d = C.DISTANCE_WEIGHT * d_by_dist + (1 - C.DISTANCE_WEIGHT) * d_by_stop
    d = np.maximum.accumulate(d)

    dlon = np.interp(d, cum_dist, lons)
    dlat = np.interp(d, cum_dist, lats)

    # ---- adaptive zoom from per-frame angular speed, percentile-normalised so
    # it behaves the same whether the dataset is 40 Costcos or 1,300 McDonald's
    step = np.hypot(np.gradient(dlon), np.gradient(dlat) * np.cos(np.radians(dlat)))
    step = _gauss_smooth(step, C.FPS * 0.5)
    raw = step * C.FPS * C.LOOKAHEAD_SEC
    med = max(np.median(raw), 1e-9)
    hw = med * (raw / med) ** C.ZOOM_RESPONSE
    hw = np.clip(hw, C.ZOOM_MIN_DEG, C.ZOOM_MAX_DEG)

    sig = C.CAMERA_SMOOTH_SEC * C.FPS
    dlon = _gauss_smooth(dlon, sig)
    dlat = _gauss_smooth(dlat, sig)
    hw = np.exp(_gauss_smooth(np.log(hw), sig * 1.4))

    # ---- HOOK / WHIP / REVEAL built around the smoothed drive endpoints
    W, E, S, N = extent
    wide_hw = wide_half_width(extent, C.OUT_W, C.OUT_H)
    hw = np.minimum(hw, wide_hw * 0.92)   # never out-zoom the establishing shot
    wide_lon, wide_lat = (W + E) / 2, (S + N) / 2
    # 9:16 leaves dead space above and below a wide region; lifting the map into
    # the upper two thirds keeps the stats block off the route.
    wide_half_h = wide_hw * (C.OUT_H / C.OUT_W) * np.cos(np.radians(wide_lat))
    wide_lat -= C.WIDE_SHOT_LIFT * wide_half_h

    def blend(t, a, b):
        return a + (b - a) * t

    hook_lon = np.full(n_hook, wide_lon)
    hook_lat = np.full(n_hook, wide_lat)
    hook_hw = np.full(n_hook, wide_hw)
    if n_hook:  # slow creeping push-in so the wide shot isn't a still frame
        z = np.linspace(0, 1, n_hook)
        hook_hw = wide_hw * (1 - 0.05 * z)

    t = _smoothstep(np.linspace(0, 1, n_whip)) if n_whip else np.zeros(0)
    whip_lon = blend(t, hook_lon[-1] if n_hook else wide_lon, dlon[0])
    whip_lat = blend(t, hook_lat[-1] if n_hook else wide_lat, dlat[0])
    whip_hw = np.exp(blend(t, np.log(hook_hw[-1] if n_hook else wide_hw), np.log(hw[0])))

    t = _smoothstep(np.linspace(0, 1, n_reveal)) if n_reveal else np.zeros(0)
    rev_lon = blend(t, dlon[-1], wide_lon)
    rev_lat = blend(t, dlat[-1], wide_lat)
    rev_hw = np.exp(blend(t, np.log(hw[-1]), np.log(wide_hw)))

    cam_lon = np.concatenate([hook_lon, whip_lon, dlon, rev_lon])
    cam_lat = np.concatenate([hook_lat, whip_lat, dlat, rev_lat])
    cam_hw = np.concatenate([hook_hw, whip_hw, hw, rev_hw])

    # ---- arc distance per frame (HUD reads from this, not from the camera)
    arc = np.concatenate([
        np.zeros(n_hook + n_whip),
        d,
        np.full(n_reveal, total),
    ])

    phase = np.array(["hook"] * n_hook + ["whip"] * n_whip +
                     ["drive"] * n_drive + ["reveal"] * n_reveal)
    ptime = np.concatenate([
        np.linspace(0, 1, n_hook) if n_hook else np.zeros(0),
        np.linspace(0, 1, n_whip) if n_whip else np.zeros(0),
        np.linspace(0, 1, n_drive),
        np.linspace(0, 1, n_reveal) if n_reveal else np.zeros(0),
    ])

    return {
        "lon": cam_lon, "lat": cam_lat, "hw": cam_hw,
        "arc": arc, "phase": phase, "ptime": ptime,
        "n": total_frames, "wide_hw": wide_hw,
    }


def _smoothstep(t):
    return t * t * (3 - 2 * t)
