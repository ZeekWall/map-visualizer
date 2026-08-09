"""Auto-starts the local OSRM routing container so the TUI doesn't require a
manual `scripts/osrm-setup.sh ... ` left running in another terminal.

Only ever touches a single, fixed-name container (reused/restarted across
calls instead of creating a new one each time) and never stops it -- once
started it's left running for other tools (main.py, another TUI session) to
use, even after this process exits. Serving a *different* region than what's
currently running still means re-running scripts/osrm-setup.sh with a new
extract and restarting the container by hand, same limitation the setup
script itself documents; this only automates "start what's already built."
"""

import glob
import os
import shutil
import subprocess
import time
from urllib.parse import urlparse

import requests

import config as C

CONTAINER_NAME = "map-viz-osrm"
OSRM_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "osrm")
IMAGE = "osrm/osrm-backend"


def _port():
    return urlparse(C.OSRM_URL).port or 5000


def _pick_graph():
    """Path to the single .osrm graph file to serve, or None if none built
    yet (glob "*.osrm" -- not "*.osrm.*" -- so this only matches the main
    graph file, not its many .osrm.cells/.osrm.mldgr/... shards)."""
    graphs = sorted(glob.glob(os.path.join(OSRM_DIR, "*.osrm")))
    if not graphs:
        return None
    if len(graphs) == 1:
        return graphs[0]
    # more than one region built -- prefer the one matching the currently
    # configured region, else just take the first and let the caller's log
    # message make the ambiguity visible.
    slug = C.REGION_NAME.lower().replace(" ", "")
    for g in graphs:
        if slug in os.path.basename(g).lower():
            return g
    return graphs[0]


def _container_state():
    """"running" | "stopped" | "missing" | "docker-error"."""
    r = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER_NAME],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return "missing"
    return "running" if r.stdout.strip() == "true" else "stopped"


def _wait_reachable(timeout_sec=10):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            requests.get(C.OSRM_URL, timeout=1)
            return True
        except requests.RequestException:
            time.sleep(0.5)
    return False


def ensure_running():
    """Best-effort: start (or resume) the OSRM container if it isn't already
    up. Never raises -- routing.py already falls back to straight legs /
    surfaces a clear error if OSRM ends up unreachable regardless of why.

    Returns a short human-readable status string for a startup toast/log.
    """
    if not C.ROUTING_ENABLED:
        return "routing disabled (C.ROUTING_ENABLED=False), skipping OSRM"

    if shutil.which("docker") is None:
        return "docker not found on PATH -- OSRM not started"

    state = _container_state()
    if state == "running":
        return "OSRM already running"

    if state == "stopped":
        r = subprocess.run(["docker", "start", CONTAINER_NAME],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return f"failed to resume OSRM container: {r.stderr.strip()[:200]}"
        ok = _wait_reachable()
        return "OSRM resumed" if ok else "OSRM resumed, not yet reachable (still loading?)"

    graph = _pick_graph()
    if graph is None:
        return "no OSRM graph in osrm/ -- run scripts/osrm-setup.sh first, routing will fall back to straight lines"

    r = subprocess.run(
        ["docker", "run", "-d", "--name", CONTAINER_NAME,
         "-p", f"{_port()}:5000", "-v", f"{OSRM_DIR}:/data", IMAGE,
         "osrm-routed", "--algorithm", "mld", f"/data/{os.path.basename(graph)}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return f"failed to start OSRM container: {r.stderr.strip()[:200]}"
    ok = _wait_reachable()
    served = os.path.basename(graph)
    return f"OSRM started, serving {served}" if ok else \
        f"OSRM container started (serving {served}), not yet reachable (still loading?)"
