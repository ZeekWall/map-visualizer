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
    n_hook = min(int(round(C.FPS * C.HOOK_SEC)), total_frames)
    remaining = total_frames - n_hook
    act_total = C.ACT_WHIP + C.ACT_DRIVE + C.ACT_REVEAL
    n_whip = int(remaining * C.ACT_WHIP / act_total)
    n_reveal = int(remaining * C.ACT_REVEAL / act_total)
    n_drive = remaining - n_whip - n_reveal

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

    # camera holds static on the wide shot -- the breathing loop (see render.py)
    # already carries the hook's motion, so the zoom-in is saved for the whip
    hook_lon = np.full(n_hook, wide_lon)
    hook_lat = np.full(n_hook, wide_lat)
    hook_hw = np.full(n_hook, wide_hw)

    # both ends of the whip hold a duplicate frame (t=0 repeats the hook's
    # last frame, t=1 repeats the drive's first frame) -- a 1-frame hold is
    # imperceptible at 30fps, and keeping both endpoints in the array is what
    # lets the Hermite tangent below be matched exactly at the last rendered
    # frame; excluding an endpoint to dodge the hold reintroduces a small
    # velocity gap right where it matters most
    whip_lon0 = hook_lon[-1] if n_hook else wide_lon
    whip_lat0 = hook_lat[-1] if n_hook else wide_lat
    whip_hw0 = hook_hw[-1] if n_hook else wide_hw
    t = np.linspace(0, 1, n_whip) if n_whip else np.zeros(0)
    # cubic Hermite, velocity-matched at both ends: the hook is static (m0=0)
    # and the drive is already moving at full speed at its first frame (m1 =
    # the drive's own per-frame step, scaled into whip's t-space) -- this is
    # what removes the "camera slams to a stop" beat at the whip/drive seam
    m1_lon = (dlon[1] - dlon[0]) * n_whip if n_whip else 0.0
    m1_lat = (dlat[1] - dlat[0]) * n_whip if n_whip else 0.0
    m1_lhw = (np.log(hw[1]) - np.log(hw[0])) * n_whip if n_whip else 0.0
    whip_lon = _hermite(t, whip_lon0, dlon[0], 0.0, m1_lon)
    whip_lat = _hermite(t, whip_lat0, dlat[0], 0.0, m1_lat)
    whip_hw = np.exp(_hermite(t, np.log(whip_hw0), np.log(hw[0]), 0.0, m1_lhw))

    # stop one step short of t=1: that final step is supplied by the wrap back
    # to frame 0 (which sits at the same wide_lon/wide_lat/wide_hw), so the
    # loop doesn't hold on a duplicate frame at the seam
    t = np.linspace(0, 1, n_reveal + 1)[:-1] if n_reveal else np.zeros(0)
    # velocity-matched at the drive end (m0), at rest by the time it reaches
    # the wide shot (m1=0) -- removes the "camera slams to a stop" beat at
    # the drive/reveal seam
    m0_lon = (dlon[-1] - dlon[-2]) * n_reveal if n_reveal and n_drive > 1 else 0.0
    m0_lat = (dlat[-1] - dlat[-2]) * n_reveal if n_reveal and n_drive > 1 else 0.0
    m0_lhw = (np.log(hw[-1]) - np.log(hw[-2])) * n_reveal if n_reveal and n_drive > 1 else 0.0
    rev_lon = _hermite(t, dlon[-1], wide_lon, m0_lon, 0.0)
    rev_lat = _hermite(t, dlat[-1], wide_lat, m0_lat, 0.0)
    rev_hw = np.exp(_hermite(t, np.log(hw[-1]), np.log(wide_hw), m0_lhw, 0.0))

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


def _hermite(t, p0, p1, m0, m1):
    """Cubic Hermite interpolation from p0 to p1 over t in [0, 1], matching
    tangents m0/m1 (already scaled to that same t-space) at each end. Used
    instead of smoothstep at the whip/drive and drive/reveal seams so the
    camera's velocity is continuous across them instead of hitting 0."""
    t2, t3 = t * t, t * t * t
    h00 = 2 * t3 - 3 * t2 + 1
    h10 = t3 - 2 * t2 + t
    h01 = -2 * t3 + 3 * t2
    h11 = t3 - t2
    return h00 * p0 + h10 * m0 + h01 * p1 + h11 * m1
