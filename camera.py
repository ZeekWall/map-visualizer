"""Precomputes the whole camera path before a single frame is drawn.

Four acts: HOOK (wide, dots ignite) -> WHIP (fast zoom to the start pin) ->
DRIVE (follow cam) -> REVEAL (pull back to the finished loop).

Zoom during DRIVE is CAMERA_ZOOM_DEG (half-width in degrees at median drive
speed), widened on fast legs by CAMERA_ZOOM_ADAPT -- purely a look, with no
correctness role: containment is entirely position's job (see below), so zoom
never has to compromise for it.

Position is a deadzone: the head of the route is pinned inside a centered box
(CAMERA_BOX) and only pushes the camera when it reaches an edge (see
_corridor_solve), so the head is guaranteed on-screen on every drive frame --
a route that darts out to an isolated stop and back (e.g. a lone El Paso
location on an otherwise Central-Texas loop) used to lose the head off-frame
at the tip of the spur. That bug wasn't a lookahead failing to frame future
points -- it was the drive path's Gaussian smoothing (a symmetric, non-causal
filter) straddling the outbound/return legs at the turnaround, which point in
opposite directions and cancel, pulling the camera back toward the cluster
exactly when the head was furthest out. A plain positional clamp would fix
containment but hitch (velocity zero inside the box, snapping to head
velocity at the edge), so instead the box traces a corridor around the head
track and the camera solves for the smoothest path that stays inside it --
unconstrained, that solve degenerates to Gaussian-style smoothing, so
well-behaved routes look close to a plain smoothed path; only genuine spurs
actually engage the box.

Everything is smoothed in log-zoom space, which is what stops a zoom from
feeling like it snaps.
"""

import numpy as np

import config as C

# Ratio between the corridor solve's tracking weight (lam) and CAMERA_SMOOTH_SEC
# (in frames, sig = CAMERA_SMOOTH_SEC * FPS), via the smoothing-spline/Gaussian-
# kernel equivalence lam ~= k / sig**4 (Silverman '84). Calibrated against
# _gauss_smooth on synthetic tracks; k in [0.5, 1.0] all landed within a few
# percent of optimal, 0.75 sits in the middle of that range.
_CAMERA_LAMBDA_K = 0.75


def _gauss_smooth(a, sigma_frames):
    if sigma_frames < 0.5 or len(a) < 3:
        return a
    r = int(max(1, round(sigma_frames * 3)))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma_frames) ** 2)
    k /= k.sum()
    pad = np.pad(a, (r, r), mode="edge")
    return np.convolve(pad, k, mode="valid")


def _d2td2_dense(n):
    """Dense n x n matrix of D2^T D2, where D2 is the second-difference
    operator ((D2 x)_j = x_j - 2x_{j+1} + x_{j+2}). This is the natural-
    boundary discrete roughness/biharmonic penalty matrix: interior rows are
    the standard [1, -4, 6, -4, 1] stencil, the first/last two rows are the
    truncated boundary stencils that fall out of not padding D2's input."""
    M = np.zeros((n, n))
    if n < 3:
        return M
    i = np.arange(n - 2)
    M[i, i] += 1;     M[i, i + 1] += -2;   M[i, i + 2] += 1
    M[i + 1, i] += -2; M[i + 1, i + 1] += 4; M[i + 1, i + 2] += -2
    M[i + 2, i] += 1;  M[i + 2, i + 1] += -2; M[i + 2, i + 2] += 1
    return M


def _corridor_solve(head, lo, hi, lam, D2TD2, max_outer=50):
    """The smoothest path x that stays within [lo[i], hi[i]] at every frame i,
    minimizing ||D2 x||^2 + lam*||x - head||^2. Always feasible (x = head
    trivially satisfies every bound), so this can only trade smoothness for
    containment, never fail.

    An exact QP via active-set: solve the unconstrained linear system
    (D2^T D2 + lam*I) x = lam*head, pin any frame that comes out past its
    bound to that bound (row -> identity) and re-solve, repeat until no new
    pins are needed. Convex objective + box constraints means this converges
    monotonically in a small number of outer passes -- typically one per
    'reason' the corridor pinches, not one per frame.

    Plain (sub-)gradient descent on this objective was tried first and
    rejected: D2^T D2's eigenvalues span from ~0 (smooth/linear modes) to 16
    (high-frequency modes), so first-order methods need a huge iteration
    count to resolve the low-frequency (i.e. the visually important) part of
    the path. A direct solve sidesteps that entirely -- n is at most a few
    thousand frames, and dense np.linalg.solve at that size is milliseconds,
    negligible next to the render itself."""
    n = len(head)
    if n < 3:
        return np.clip(head, lo, hi)

    fixed = np.zeros(n, dtype=bool)
    fixed_val = np.zeros(n)
    base = D2TD2 + lam * np.eye(n)
    x = head
    for _ in range(max_outer):
        M = base.copy()
        rhs = lam * head
        if fixed.any():
            idx = np.where(fixed)[0]
            M[idx, :] = 0.0
            M[idx, idx] = 1.0
            rhs = rhs.copy()
            rhs[idx] = fixed_val[idx]
        x = np.linalg.solve(M, rhs)
        viol_lo = (x < lo) & ~fixed
        viol_hi = (x > hi) & ~fixed
        if not (viol_lo.any() or viol_hi.any()):
            break
        fixed |= viol_lo | viol_hi
        fixed_val[viol_lo] = lo[viol_lo]
        fixed_val[viol_hi] = hi[viol_hi]
    return np.clip(x, lo, hi)


def _assert_contained(drive_lon, drive_lat, lo_lon, hi_lon, lo_lat, hi_lat, eps=1e-6):
    bad_lon = (drive_lon < lo_lon - eps) | (drive_lon > hi_lon + eps)
    bad_lat = (drive_lat < lo_lat - eps) | (drive_lat > hi_lat + eps)
    bad = bad_lon | bad_lat
    if bad.any():
        i = int(np.argmax(bad))
        raise RuntimeError(
            f"camera deadzone violated at drive frame {i}: "
            f"lon={drive_lon[i]:.5f} (bounds {lo_lon[i]:.5f}..{hi_lon[i]:.5f}), "
            f"lat={drive_lat[i]:.5f} (bounds {lo_lat[i]:.5f}..{hi_lat[i]:.5f}). "
            f"This should be unreachable -- the corridor solve is always "
            f"feasible, so this means a step upstream of it regressed."
        )


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

    W, E, S, N = extent
    wide_hw = wide_half_width(extent, C.OUT_W, C.OUT_H)

    # ---- DRIVE: arc position blended between constant-speed and constant-stops
    u = np.linspace(0.0, 1.0, n_drive)
    d_by_dist = u * total
    d_by_stop = np.interp(u * n_stops, np.arange(n_stops + 1), stop_dist)
    d = C.DISTANCE_WEIGHT * d_by_dist + (1 - C.DISTANCE_WEIGHT) * d_by_stop
    d = np.maximum.accumulate(d)

    # exact head-of-route track -- never smoothed. The camera tracks this, it
    # never becomes it: smoothing goes into the corridor solve below instead.
    head_lon = np.interp(d, cum_dist, lons)
    head_lat = np.interp(d, cum_dist, lats)

    # ---- zoom: CAMERA_ZOOM_DEG at median drive speed, widened on fast legs
    # by CAMERA_ZOOM_ADAPT (0 = constant zoom, 1 = half-width proportional to
    # speed). Percentile-normalised so it behaves the same whether the
    # dataset is 40 Costcos or 1,300 McDonald's. Purely a look -- containment
    # is entirely position's job below, so zoom never has to compromise for it.
    step = np.hypot(np.gradient(head_lon), np.gradient(head_lat) * np.cos(np.radians(head_lat)))
    step = _gauss_smooth(step, C.FPS * 0.5)
    med = max(np.median(step), 1e-9)
    # clip is a degeneracy guard against a near-stationary head driving hw -> 0,
    # not a tuning parameter
    norm = np.clip(step / med, 0.2, 5.0)
    hw = C.CAMERA_ZOOM_DEG * norm ** C.CAMERA_ZOOM_ADAPT
    hw = np.minimum(hw, wide_hw * 0.92)   # never out-zoom the establishing shot

    sig = C.CAMERA_SMOOTH_SEC * C.FPS
    hw = np.exp(_gauss_smooth(np.log(hw), sig * 1.4))

    # ---- position: the smoothest path that keeps the head inside a centered
    # box at every frame (see _corridor_solve). The box is sized off the
    # already-smoothed hw, so pan only ever responds to zoom, never the
    # reverse -- there's no feedback loop between the two solves.
    r_lon = C.CAMERA_BOX * hw
    r_lat = C.CAMERA_BOX * hw * (C.OUT_H / C.OUT_W) * np.cos(np.radians(head_lat))
    lo_lon, hi_lon = head_lon - r_lon, head_lon + r_lon
    lo_lat, hi_lat = head_lat - r_lat, head_lat + r_lat

    lam = _CAMERA_LAMBDA_K / max(sig, 1e-6) ** 4
    D2TD2 = _d2td2_dense(n_drive)
    drive_lon = _corridor_solve(head_lon, lo_lon, hi_lon, lam, D2TD2)
    drive_lat = _corridor_solve(head_lat, lo_lat, hi_lat, lam, D2TD2)
    _assert_contained(drive_lon, drive_lat, lo_lon, hi_lon, lo_lat, hi_lat)

    # ---- HOOK / WHIP / REVEAL built around the solved drive endpoints
    wide_lon, wide_lat = (W + E) / 2, (S + N) / 2
    # 9:16 leaves dead space above and below a wide region; lifting the map into
    # the upper two thirds keeps the stats block off the route.
    wide_half_h = wide_hw * (C.OUT_H / C.OUT_W) * np.cos(np.radians(wide_lat))
    wide_lat -= C.WIDE_SHOT_LIFT * wide_half_h

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
    m1_lon = (drive_lon[1] - drive_lon[0]) * n_whip if n_whip else 0.0
    m1_lat = (drive_lat[1] - drive_lat[0]) * n_whip if n_whip else 0.0
    m1_lhw = (np.log(hw[1]) - np.log(hw[0])) * n_whip if n_whip else 0.0
    whip_lon = _hermite(t, whip_lon0, drive_lon[0], 0.0, m1_lon)
    whip_lat = _hermite(t, whip_lat0, drive_lat[0], 0.0, m1_lat)
    whip_hw = np.exp(_hermite(t, np.log(whip_hw0), np.log(hw[0]), 0.0, m1_lhw))

    # the reveal splits into an EASE sub-phase (camera pulls back to the wide
    # shot) and a HOLD sub-phase (sits there) -- REVEAL_EASE_FRAC controls the
    # split, so the pullback can be quick while the finished loop still gets
    # real screen time before the seamless wrap back to the hook
    n_reveal_ease = max(1, int(round(n_reveal * C.REVEAL_EASE_FRAC))) if n_reveal else 0
    n_reveal_hold = n_reveal - n_reveal_ease

    t = np.linspace(0, 1, n_reveal_ease) if n_reveal_ease else np.zeros(0)
    # velocity-matched at the drive end (m0), at rest by the time it reaches
    # the wide shot (m1=0) -- removes the "camera slams to a stop" beat at
    # the drive/reveal seam
    m0_lon = (drive_lon[-1] - drive_lon[-2]) * n_reveal_ease if n_reveal_ease and n_drive > 1 else 0.0
    m0_lat = (drive_lat[-1] - drive_lat[-2]) * n_reveal_ease if n_reveal_ease and n_drive > 1 else 0.0
    m0_lhw = (np.log(hw[-1]) - np.log(hw[-2])) * n_reveal_ease if n_reveal_ease and n_drive > 1 else 0.0
    ease_lon = _hermite(t, drive_lon[-1], wide_lon, m0_lon, 0.0)
    ease_lat = _hermite(t, drive_lat[-1], wide_lat, m0_lat, 0.0)
    ease_hw = np.exp(_hermite(t, np.log(hw[-1]), np.log(wide_hw), m0_lhw, 0.0))

    # HOLD repeats the exact wide shot -- identical to the hook's own hold, so
    # the run of static frames flows straight through the loop wrap with
    # nothing to seam
    rev_lon = np.concatenate([ease_lon, np.full(n_reveal_hold, wide_lon)])
    rev_lat = np.concatenate([ease_lat, np.full(n_reveal_hold, wide_lat)])
    rev_hw = np.concatenate([ease_hw, np.full(n_reveal_hold, wide_hw)])

    cam_lon = np.concatenate([hook_lon, whip_lon, drive_lon, rev_lon])
    cam_lat = np.concatenate([hook_lat, whip_lat, drive_lat, rev_lat])
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
