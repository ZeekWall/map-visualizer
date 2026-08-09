"""Textual workbench: target/region picker, live 6-stage progress, a knob
editor for everything in config.py, a cover preview, and cache management.

    python tui.py

Requires textual + textual-image (pip install -r requirements.txt). See
tui/app.py for the app itself; this is just the entry point.
"""

import sys

# Knobs pane edits config.py on disk and then importlib.reload()s it in this
# process (see tui/panes.py). CPython's default .pyc staleness check
# truncates the source mtime to whole seconds, so two edits landing in the
# same wall-clock second with the same resulting file size (e.g. 0.15 ->
# 0.18, same width) can make reload() silently serve the pre-edit bytecode.
# Disabling bytecode caching for the whole process sidesteps that entirely;
# config.py is tiny and only reloaded on an interactive edit, so there's no
# real cost to paying the parse every time.
sys.dont_write_bytecode = True

from tui.app import run

if __name__ == "__main__":
    run()
