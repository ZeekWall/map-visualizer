"""Textual workbench: target/region picker, live 6-stage progress, a knob
editor for everything in config.py, a cover preview, and cache management.

    python tui.py

Requires textual + textual-image (pip install -r requirements.txt). See
tui/app.py for the app itself; this is just the entry point.
"""

from tui.app import run

if __name__ == "__main__":
    run()
