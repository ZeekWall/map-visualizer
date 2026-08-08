"""Minimal terminal progress reporting for the render pipeline: step headers,
animated spinners for indeterminate work, and progress bars for determinate
work. No third-party dependency -- reuses the \\r + elapsed/ETA idiom that used
to live only in render.py's frame counter.

Design constraints:
- exactly one line per stage when stdout is not a TTY (piped/redirected) --
  no \\r, no animation, ever, in that case.
- when it IS a TTY, only the current step's line animates in place; completed
  steps stay in scrollback.
- cache-hit paths stay silent/instant -- they call done() directly without
  ever touching a spinner or bar.

Usage: main.py calls step(i, n, name) before each pipeline stage; the stage's
own module calls spinner()/bar() for the slow path and done() to finalise,
mirroring how each module already owned its own status prints.
"""

import itertools
import sys
import threading
import time

enabled = True

_TTY = sys.stdout.isatty()
_SPIN_FRAMES = "|/-\\"
_REDRAW_SEC = 0.1  # ~10fps cap on redraws, so per-frame/per-move updates don't flood the terminal

_state = {"prefix": "", "start": 0.0}


def _out(s, nl=False):
    if not enabled or not _TTY:
        return
    sys.stdout.write("\r\x1b[K" + s)
    if nl:
        sys.stdout.write("\n")
    sys.stdout.flush()


def step(i, n, name):
    """Announce stage i/n. Follow with done(), and optionally a spinner()/bar()
    in between for the slow path."""
    _state["prefix"] = f"[{i}/{n}] {name:<8} "
    _state["start"] = time.time()
    _out(_state["prefix"])


def done(summary):
    """Finalise the current step's line. Safe to call directly after step()
    with nothing in between (the cache-hit path)."""
    if not enabled:
        return
    line = f"{_state['prefix']}done  {summary}"
    if _TTY:
        _out(line, nl=True)
    else:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def warn(msg):
    """Print a standalone warning line without finalising the current step
    (unlike done()) -- for a non-fatal hiccup mid-step, e.g. a source
    falling back to another. Always visible, TTY or not.
    """
    if not enabled:
        return
    if _TTY:
        _out(f"  ! {msg}", nl=True)
        _out(_state["prefix"])
    else:
        sys.stdout.write(f"  ! {msg}\n")
        sys.stdout.flush()


class _Spinner:
    def __init__(self, text):
        self.text = text
        self._stop = threading.Event()
        self._thread = None

    def _run(self):
        for frame in itertools.cycle(_SPIN_FRAMES):
            if self._stop.is_set():
                return
            el = time.time() - _state["start"]
            _out(f"{_state['prefix']}{frame} {self.text}  {el:.0f}s")
            time.sleep(_REDRAW_SEC)

    def __enter__(self):
        if enabled and _TTY:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        if exc_type is not None and enabled and _TTY:
            _out("", nl=True)  # clean line so the traceback doesn't collide with it
        return False  # never suppress the exception


def spinner(text):
    """Context manager: animated spinner + elapsed timer around a blocking,
    indeterminate call. `.text` can be updated from inside the `with` (e.g. a
    retry loop updating its caption). No-op when stdout isn't a TTY."""
    return _Spinner(text)


class Bar:
    def __init__(self, total, unit=""):
        self.total = max(total, 1)
        self.unit = unit
        self.n = 0
        self.t0 = time.time()
        self._last_draw = 0.0

    def update(self, n):
        self.n = n
        if not (enabled and _TTY):
            return
        now = time.time()
        if now - self._last_draw < _REDRAW_SEC and n < self.total:
            return
        self._last_draw = now
        self._draw()

    def _draw(self):
        frac = min(self.n / self.total, 1.0)
        width = 20
        filled = int(width * frac)
        bar = "#" * filled + "-" * (width - filled)
        el = time.time() - self.t0
        eta = el / max(self.n, 1e-9) * (self.total - self.n)
        _out(f"{_state['prefix']}[{bar}] {frac * 100:5.1f}%  "
            f"{self.n:,.0f}/{self.total:,.0f}{self.unit}   "
            f"{el:.0f}s elapsed  ~{eta:.0f}s left")


def bar(total, unit=""):
    """Determinate progress bar. Call .update(n) as work completes."""
    return Bar(total, unit)
