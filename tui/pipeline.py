"""Runs main.run_pipeline on a worker thread.

The pipeline (Overpass/ATP fetch, TSP solve, OSRM calls, Cartopy basemap,
frame render) is synchronous and blocking -- TSP alone is a hard
SOLVER_TIME_BUDGET-second spin (config.py) -- so it must never run on
Textual's own event loop thread. Call run() via App.run_worker(thread=True);
every progress.py event is marshalled back to the caller's `on_event`
callback exactly as reported, so the caller is responsible for hopping back
onto the UI thread (Textual's `call_from_thread`) before touching widgets.

Deliberately does NOT redirect sys.stdout/stderr: contextlib.redirect_stdout
mutates process-global state, and this function runs on a background thread
while the UI thread keeps running concurrently -- anything ELSE in the
process that prints during that window (another widget, a test harness, a
future feature) would get silently swallowed into this function's buffer
instead of going where it belongs. It isn't needed anyway: every raw
print() in the pipeline modules has already been converted to
progress.warn() (routed through `sink` below, independent of stdout), and
ffmpeg's stderr is captured separately by render.py's own pipe-drain
thread. Only warnings.catch_warnings() is used, to silence Cartopy's
cold-path DownloadWarning -- also global state, but its blast radius on a
theoretical collision is "a warning wasn't suppressed", not "output vanished".
"""

import warnings
from contextlib import contextmanager

import config as C
import progress
import main


@contextmanager
def progress_muted(on_event):
    """Swap progress.py's terminal output for `on_event`, for the duration
    of the `with` block. Shared by run() below and by the Explore tab's
    fetches (explore.py calls straight into overpassapi/atpapi, which use
    progress.spinner() the same way the pipeline does) -- any caller that
    invokes those modules from a Textual worker thread needs this same
    swap, since a spinner's background thread writing raw \\r+ANSI to
    stdout would corrupt the Textual screen. See progress.py's `sink`
    docstring for the full contract.
    """
    old_sink = progress.sink
    old_tty = progress._TTY
    progress.sink = on_event
    progress._TTY = False  # keeps the spinner thread from ever starting and
                           # makes _out()/done()/warn() skip their ANSI/\r path
    try:
        yield
    finally:
        progress.sink = old_sink
        progress._TTY = old_tty


class PipelineError(Exception):
    """Wraps whatever main.run_pipeline raised. .original is the underlying
    exception -- "cancelled" (as a RuntimeError) for a cancel via `cancel`."""

    def __init__(self, original):
        super().__init__(str(original))
        self.original = original


def run(on_event, target=None, region=None, preview=False, cover_only=False,
       refresh_places=False, refresh_roads=False, rebuild_basemap=False,
       duration=None, res=None, cancel=None):
    """Runs one full pipeline invocation. `on_event(event_dict)` is called
    for every progress.py event (see progress.py's module docstring for the
    event shapes); it fires from THIS (worker) thread.

    Returns (out_path, cover_path) on success. Raises PipelineError on
    failure -- .original is the underlying exception ("cancelled" for a
    cancel via `cancel`).
    """
    if target or region:
        C.retarget(target, region)

    # OUT_W/OUT_H/FPS/CRF/PRESET/DURATION_SEC are config.py module globals,
    # not per-call parameters -- config stays imported for the TUI's whole
    # process lifetime, so mutating them for one run (preview's 540x960@15fps,
    # a --res override) and never putting them back leaks into every later
    # run too. Toggle Preview off after a preview render and the *next*
    # "full" render would still use preview's FPS/resolution (and therefore
    # its frame count, since camera.build's frame count is FPS * duration)
    # because nothing ever restored them. Snapshot and restore exactly like
    # progress_muted below does for sink/_TTY.
    saved = {name: getattr(C, name) for name in
            ("OUT_W", "OUT_H", "FPS", "CRF", "PRESET", "DURATION_SEC")}
    if duration is not None:
        C.DURATION_SEC = duration
    if res is not None:
        C.OUT_W, C.OUT_H = res, res * 16 // 9
    if preview:
        C.OUT_W, C.OUT_H, C.FPS, C.CRF, C.PRESET = 540, 960, 15, 26, "veryfast"

    try:
        with progress_muted(on_event), warnings.catch_warnings():
            warnings.simplefilter("ignore")  # cartopy's cold-path DownloadWarning
            return main.run_pipeline(
                preview=preview, cover_only=cover_only,
                refresh_places=refresh_places, refresh_roads=refresh_roads,
                rebuild_basemap=rebuild_basemap, cancel=cancel)
    except Exception as e:
        raise PipelineError(e) from e
    finally:
        for name, value in saved.items():
            setattr(C, name, value)
