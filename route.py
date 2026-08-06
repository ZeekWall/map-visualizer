"""Route construction: haversine matrix -> nearest neighbour -> 2-opt + Or-opt.

Replaces python_tsp's generic local search. Uses candidate neighbour lists so the
cost stays flat as the stop count grows (McDonald's in Texas is ~1,300 nodes,
which the old best-improvement search could not touch in 60s).
"""

import hashlib
import json
import os
import time

import numpy as np


def haversine_matrix(coords):
    """coords: (n, 2) array of [lat, lon] in degrees. Returns km distances."""
    R = 6371.0088
    phi = np.radians(coords[:, 0])
    lam = np.radians(coords[:, 1])
    dphi = phi[:, None] - phi[None, :]
    dlam = lam[:, None] - lam[None, :]
    a = np.sin(dphi / 2) ** 2 + np.cos(phi)[:, None] * np.cos(phi)[None, :] * np.sin(dlam / 2) ** 2
    d = 2 * R * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
    np.fill_diagonal(d, 0.0)
    return d


def tour_length(D, tour):
    t = np.asarray(tour)
    return float(D[t, np.roll(t, -1)].sum())


def nearest_neighbor_tour(D, start=0):
    n = len(D)
    unvisited = np.ones(n, dtype=bool)
    unvisited[start] = False
    tour = [start]
    cur = start
    for _ in range(n - 1):
        d = np.where(unvisited, D[cur], np.inf)
        cur = int(np.argmin(d))
        unvisited[cur] = False
        tour.append(cur)
    return tour


def _candidates(D, k):
    k = min(k, len(D) - 1)
    return np.argsort(D, axis=1)[:, 1:k + 1]


def two_opt_pass(D, tour, pos, cand):
    """One best-improvement 2-opt move restricted to candidate neighbours."""
    n = len(tour)
    a = tour                      # city at index i
    b = np.roll(tour, -1)         # city at index i+1
    edge = D[a, b]                # cost of edge leaving index i

    j = pos[cand[a]]              # (n, k) tour index of each candidate
    i = np.arange(n)[:, None]

    aj = tour[j]
    bj = b[j]
    delta = D[a[:, None], aj] + D[b[:, None], bj] - edge[:, None] - edge[j]
    delta[(j <= i + 1) | (j >= n - 1) & (i == 0)] = 0.0  # skip adjacent / whole-tour reversal

    idx = int(np.argmin(delta))
    best = delta.flat[idx]
    if best >= -1e-9:
        return None
    ii, kk = divmod(idx, cand.shape[1])
    return ii, int(j[ii, kk]), float(best)


def or_opt_pass(D, tour, pos, cand, seg_lens=(1, 2, 3)):
    """Best-improvement relocation of a short segment next to a candidate city."""
    n = len(tour)
    best = (None, 0.0)
    for L in seg_lens:
        if n < L + 3:
            continue
        i = np.arange(1, n - L)                 # segment start index (keep index 0 fixed)
        s0, s1 = tour[i], tour[i + L - 1]
        p, q = tour[i - 1], tour[i + L]
        gain = D[p, s0] + D[s1, q] - D[p, q]    # saved by removing the segment

        j = pos[cand[s0]]                       # (m, k) insertion points
        c = tour[j]
        d = tour[(j + 1) % n]
        base = D[c, d]
        fwd = D[c, s0[:, None]] + D[s1[:, None], d] - base
        rev = D[c, s1[:, None]] + D[s0[:, None], d] - base
        flip = rev < fwd
        cost = np.where(flip, rev, fwd)

        delta = cost - gain[:, None]
        overlap = (j >= i[:, None] - 1) & (j <= i[:, None] + L - 1)
        delta[overlap] = 0.0

        idx = int(np.argmin(delta))
        if delta.flat[idx] < best[1] - 1e-9:
            r, kk = divmod(idx, cand.shape[1])
            best = ((int(i[r]), L, int(j[r, kk]), bool(flip[r, kk])), float(delta.flat[idx]))
    return best[0]


def optimize(D, tour, time_budget=45.0, k=12, verbose=True):
    tour = np.asarray(tour, dtype=np.int64)
    n = len(tour)
    cand = _candidates(D, k)
    pos = np.empty(n, dtype=np.int64)
    pos[tour] = np.arange(n)

    t0 = time.time()
    best_len = tour_length(D, tour)
    start_len = best_len
    moves = 0

    while time.time() - t0 < time_budget:
        mv = two_opt_pass(D, tour, pos, cand)
        if mv is not None:
            i, j, _ = mv
            tour[i + 1:j + 1] = tour[i + 1:j + 1][::-1]
            pos[tour] = np.arange(n)
            moves += 1
            continue

        mv = or_opt_pass(D, tour, pos, cand)
        if mv is None:
            break
        i, L, j, flip = mv
        seg = list(tour[i:i + L])
        if flip:
            seg = seg[::-1]
        rest = list(tour[:i]) + list(tour[i + L:])
        anchor = tour[j]
        at = rest.index(anchor) + 1
        tour = np.array(rest[:at] + seg + rest[at:], dtype=np.int64)
        pos[tour] = np.arange(n)
        moves += 1

    best_len = tour_length(D, tour)
    if verbose:
        print(f"  {n} stops | {start_len:,.0f} km -> {best_len:,.0f} km "
              f"({100 * (1 - best_len / start_len):.1f}% better, {moves} moves, "
              f"{time.time() - t0:.0f}s)")
    return tour.tolist(), best_len


def rotate_start(coords, tour, mode="south"):
    """Rotate the cycle so it opens somewhere legible instead of stop #0."""
    t = np.asarray(tour)
    pts = coords[t]
    if mode == "south":
        s = int(np.argmin(pts[:, 0]))
    elif mode == "north":
        s = int(np.argmax(pts[:, 0]))
    elif mode == "west":
        s = int(np.argmin(pts[:, 1]))
    elif mode == "east":
        s = int(np.argmax(pts[:, 1]))
    else:
        s = int(mode)
    return np.roll(t, -s).tolist()


def solve(coords, time_budget=45.0, cache_dir="cache/route", start="south"):
    os.makedirs(cache_dir, exist_ok=True)
    key = hashlib.sha1(
        np.round(coords, 6).tobytes() + f"{time_budget}{start}".encode()
    ).hexdigest()[:12]
    path = os.path.join(cache_dir, f"{key}.json")

    D = haversine_matrix(coords)
    if os.path.exists(path):
        with open(path) as f:
            blob = json.load(f)
        print(f"  route cache hit ({blob['length_km']:,.0f} km)")
        return blob["tour"], D, blob["length_km"]

    print("Solving TSP...")
    t0 = nearest_neighbor_tour(D, start=0)
    tour, length = optimize(D, t0, time_budget=time_budget)
    tour = rotate_start(coords, tour, start)

    with open(path, "w") as f:
        json.dump({"tour": tour, "length_km": length}, f)
    return tour, D, length
