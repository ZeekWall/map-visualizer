"""Textual workbench for map-visualizer. Run with `python tui.py` from the
repo root (the thin launcher just does `from tui.app import run; run()`).

Only one pipeline may run at a time -- config.py is mutated at runtime
(C.retarget, C.OUT_W, ...) by tui.pipeline.run, exactly like main.py already
does, so a second concurrent run would race the first's config state. The
Run tab's Render/Cover buttons are disabled for the duration of a run to
enforce that; Cancel sets the threading.Event the pipeline checks between
stages and once per rendered frame.

The Explore tab's fetches (Overpass/All The Places/GitHub, via explore.py)
share that same constraint -- progress.py's sink/_TTY are process-global,
same as config.py -- so pipeline and explore work share the one
`_worker_running` flag below and never run concurrently, even though
explore work runs in its own Textual worker group ("explore") so it isn't
cancelled by the pipeline's `exclusive=True` default-group worker.
"""

import importlib
import os
import shutil
import threading
import time

from textual.app import App, ComposeResult
from textual.widgets import Button, Footer, Header, Static, TabbedContent, TabPane

import config as C
import explore
import progress
import targets
from tui.panes import CachePane, ExplorePane, KnobsPane, OutputPane, RunPane
from tui.pipeline import PipelineError, progress_muted, run as run_pipeline


CSS = """
Screen { layout: vertical; }
/* Textual's Horizontal defaults to height: 1fr (fills whatever space is
  left in its parent). Every row below needs an explicit height or it
  greedily eats the rest of the tab and pushes later rows (the progress
  bar, in particular) off-screen with nothing to scroll it back into view. */
#run-pickers, #run-options, #run-buttons { height: 3; }
#run-pickers Select { width: 1fr; }
#run-options Label { width: auto; padding: 1 1 0 1; }
#run-options Input, #run-options Select { width: 1fr; }
#run-buttons Button { width: 1fr; }
#stages { height: auto; padding: 1 0; }
.stage-idle { color: $text-muted; }
.stage-active { color: $warning; text-style: bold; }
.stage-done { color: $success; }
.stage-warn { color: $error; }
#render-bar { height: 1; margin: 1 0; }
.knob-row { height: 3; align: left middle; }
.knob-label { width: 34; }
KnobsPane Input { width: 1fr; }
#renders-table { height: 1fr; }
/* CachePane stacks two tables (categories, then a category's individual
  files once one is selected) -- #cache-table gets a fixed height sized to
  DIRS (5 rows + header) rather than 1fr, so the files table below it (and
  its own purge button) actually get room instead of being squeezed out. */
#cache-table { height: 7; }
#cache-buttons { height: 3; }
#cache-files-label { height: 1; padding: 1 0 0 1; }
#cache-files-table { height: 1fr; }
/* #explore-status is docked at the top of ExplorePane, outside
  #explore-scroll, so it stays visible (search result, probe result, add
  confirmation/error) no matter how far the user has scrolled the tables
  and Add form below it -- see ExplorePane's docstring. */
ExplorePane { layout: vertical; }
#explore-status { dock: top; height: 1; padding: 0 1; background: $panel; }
#explore-scroll { height: 1fr; }
#explore-search { height: 3; }
#explore-search Input { width: 1fr; }
#explore-search Select { width: 20; }
#atp-row { height: 3; align: left middle; }
#atp-row Label { width: 1fr; padding: 1 0 0 1; }
#osm-table, #brand-table, #atp-table, #explore-sample { height: 8; margin-bottom: 1; }
.add-row { height: 3; align: left middle; }
.add-label { width: auto; padding: 1 1 0 1; }
.add-row Input { width: 1fr; }
"""


class WorkbenchApp(App):
    CSS = CSS
    TITLE = "map-visualizer"
    BINDINGS = [
        ("r", "render", "Render"),
        ("c", "cover_only", "Cover only"),
        ("x", "cancel", "Cancel"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self._cancel_event = None
        self._worker_running = False
        self._explore_cancel_event = None
        self._pipeline_start = None

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="run"):
            with TabPane("Run", id="run"):
                yield RunPane()
            with TabPane("Explore", id="explore"):
                yield ExplorePane()
            with TabPane("Knobs", id="knobs"):
                yield KnobsPane()
            with TabPane("Output", id="output"):
                yield OutputPane()
            with TabPane("Cache", id="cache"):
                yield CachePane()
        yield Footer()

    def on_mount(self):
        self.query_one(OutputPane).refresh_table()
        self.query_one(CachePane).refresh_table()

    # ---- Run tab wiring -------------------------------------------------

    def on_button_pressed(self, event):
        bid = event.button.id
        if bid == "render-btn":
            self.action_render()
        elif bid == "cover-btn":
            self.action_cover_only()
        elif bid == "cancel-btn":
            self.action_cancel()
        elif bid == "refresh-outputs-btn":
            self.query_one(OutputPane).refresh_table()
        elif bid == "refresh-cache-btn":
            self.query_one(CachePane).refresh_table()
        elif bid == "purge-cache-btn":
            self._purge_selected_cache()
        elif bid == "purge-cache-file-btn":
            self._purge_selected_cache_file()
        elif bid == "explore-search-btn":
            self._start_explore_search()
        elif bid == "atp-probe-btn":
            self._start_atp_probe()
        elif bid == "add-target-btn":
            self._add_explore_target()
        elif bid == "explore-cancel-btn":
            self._cancel_explore()

    def action_render(self):
        self._start_pipeline(cover_only=False)

    def action_cover_only(self):
        self._start_pipeline(cover_only=True)

    def action_cancel(self):
        if self._cancel_event is not None:
            self._cancel_event.set()

    def _cancel_explore(self):
        if self._explore_cancel_event is None:
            return
        self._explore_cancel_event.set()
        pane = self.query_one(ExplorePane)
        # Immediate acknowledgment that the click registered -- the
        # underlying fetch can only be interrupted between retry attempts
        # (see overpassapi.run_query's docstring), so for a request that's
        # already in flight there's a real gap between clicking Cancel and
        # _explore_search_done/_atp_probe_done actually firing. Without
        # this the button looked like it was doing nothing.
        pane.query_one("#explore-cancel-btn", Button).disabled = True
        pane.status("cancelling...")

    def _start_pipeline(self, cover_only):
        if self._worker_running:
            return
        run_pane = self.query_one(RunPane)
        target = self.query_one("#target-select").value
        region = self.query_one("#region-select").value
        preview = self.query_one("#preview-switch").value
        res = self.query_one("#res-select").value
        try:
            duration = float(self.query_one("#duration-input").value)
        except ValueError:
            duration = None

        run_pane.reset_stages()
        run_pane.set_running(True)
        run_pane.status("running...")
        self._worker_running = True
        self._pipeline_start = time.time()
        self.query_one(ExplorePane).set_busy(True)
        self._cancel_event = threading.Event()
        cancel_event = self._cancel_event

        def on_event(ev):
            self.call_from_thread(self._handle_pipeline_event, ev)

        def work():
            try:
                out, cover = run_pipeline(
                    on_event, target=target, region=region, preview=preview,
                    cover_only=cover_only, duration=duration, res=res,
                    cancel=cancel_event,
                )
                self.call_from_thread(self._pipeline_done, out, cover, None)
            except PipelineError as e:
                self.call_from_thread(self._pipeline_done, None, None, e)

        self.run_worker(work, thread=True, exclusive=True)

    def _handle_pipeline_event(self, ev):
        run_pane = self.query_one(RunPane)
        kind = ev.get("kind")
        name = ev.get("name")
        i = ev.get("i")
        if kind == "step":
            run_pane.set_stage(i, name, "active")
        elif kind == "done":
            run_pane.set_stage(i, name, "done", ev.get("summary", ""))
        elif kind == "warn":
            run_pane.set_stage(i, name, "warn", ev.get("msg", ""))
        elif kind == "bar":
            run_pane.set_bar(ev.get("n", 0), ev.get("total", 1), ev.get("unit", ""))
        elif kind == "spinner":
            run_pane.set_stage(i, name, "active", ev.get("text", ""))

    def _pipeline_done(self, out, cover, error):
        run_pane = self.query_one(RunPane)
        run_pane.set_running(False)
        self._worker_running = False
        self.query_one(ExplorePane).set_busy(False)
        self._cancel_event = None
        elapsed = progress.format_elapsed(time.time() - self._pipeline_start) \
            if self._pipeline_start is not None else "?"
        self._pipeline_start = None

        if error is not None:
            run_pane.stop_bar()
            if str(error.original) == "cancelled":
                run_pane.status(f"cancelled after {elapsed}")
            else:
                run_pane.status(f"[red]failed after {elapsed}: {error.original}[/]")
            return

        run_pane.status(f"done in {elapsed} -> {out}" + (f" (cover -> {cover})" if cover else ""))
        self.query_one(OutputPane).refresh_table()

    # ---- Explore tab wiring -----------------------------------------------
    # Runs in the "explore" worker group so it isn't cancelled by the
    # pipeline's exclusive=True default-group worker, but is still gated on
    # the same _worker_running flag as the pipeline (see the module
    # docstring) since both mutate progress.py's process-global sink/_TTY.

    def _start_explore_search(self):
        if self._worker_running:
            return
        pane = self.query_one(ExplorePane)
        query = pane.query_one("#explore-query").value.strip()
        region_code = pane.query_one("#explore-region").value
        if not query:
            pane.status("enter a brand name first")
            return

        pane.status("searching...")
        self._worker_running = True
        self.query_one(RunPane).set_blocked(True)
        pane.set_busy(True)
        self._explore_cancel_event = threading.Event()
        cancel_event = self._explore_cancel_event

        def work():
            try:
                with progress_muted(lambda ev: None):
                    name, state, extent = targets.resolve_region(region_code, C.REGION_PAD_DEG)
                    if cancel_event.is_set():
                        raise RuntimeError("cancelled")
                    osm = explore.osm_probe(query, name, cancel=cancel_event)
                    spiders = explore.search_spiders(query, cancel=cancel_event)
                self.call_from_thread(
                    self._explore_search_done, osm, spiders, state, extent, name, None)
            except Exception as e:
                self.call_from_thread(
                    self._explore_search_done, None, None, None, None, None, e)

        self.run_worker(work, thread=True, group="explore")

    def _explore_search_done(self, osm, spiders, region_state, region_extent, region_name, error):
        pane = self.query_one(ExplorePane)
        self._worker_running = False
        self.query_one(RunPane).set_blocked(False)
        pane.set_busy(False)
        self._explore_cancel_event = None

        if error is not None:
            pane.status("cancelled" if str(error) == "cancelled" else f"[red]search failed: {error}[/]")
            return

        pane._region_state = region_state
        pane._region_extent = region_extent
        pane._region_name = region_name
        pane.show_osm_result(osm)
        pane.show_spider_candidates(spiders)
        cached_note = " (cached)" if osm.cached else ""
        pane.status(f"{osm.total} OSM matches{cached_note} -- {len(spiders)} spider candidate(s)")

    def _start_atp_probe(self):
        if self._worker_running:
            return
        pane = self.query_one(ExplorePane)
        table = pane.query_one("#atp-table")
        if table.cursor_row is None or table.row_count == 0:
            pane.status("select a spider candidate first")
            return
        spider = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        if pane._region_state is None:
            pane.status("search first so a region is resolved")
            return

        pane.status(f"probing {spider}...")
        self._worker_running = True
        self.query_one(RunPane).set_blocked(True)
        pane.set_busy(True)
        self._explore_cancel_event = threading.Event()
        cancel_event = self._explore_cancel_event
        region_state, region_extent = pane._region_state, pane._region_extent

        def work():
            try:
                with progress_muted(lambda ev: None):
                    probe = explore.atp_probe(spider, region_state, region_extent, cancel=cancel_event)
                self.call_from_thread(self._atp_probe_done, probe, None)
            except Exception as e:
                self.call_from_thread(self._atp_probe_done, None, e)

        self.run_worker(work, thread=True, group="explore")

    def _atp_probe_done(self, probe, error):
        pane = self.query_one(ExplorePane)
        self._worker_running = False
        self.query_one(RunPane).set_blocked(False)
        pane.set_busy(False)
        self._explore_cancel_event = None

        if error is not None:
            pane.status("cancelled" if str(error) == "cancelled" else f"[red]probe failed: {error}[/]")
            return

        pane.show_atp_probe(probe)
        pane.status(f"{probe.spider}: {probe.total} in-region -- "
                    f"{probe.instore_count} would be dropped by include_instore=False")

    def _add_explore_target(self):
        pane = self.query_one(ExplorePane)
        if pane.do_add_target():
            self._refresh_target_select()

    def _refresh_target_select(self):
        importlib.reload(targets)
        select = self.query_one("#target-select")
        select.set_options([(f"{k} -- {t.name}", k) for k, t in sorted(targets.TARGETS.items())])

    def _purge_selected_cache(self):
        table = self.query_one("#cache-table")
        if table.cursor_row is None:
            return
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        path = row_key.value
        if path and os.path.isdir(path):
            shutil.rmtree(path)
            os.makedirs(path, exist_ok=True)
        self.query_one(CachePane).refresh_table()

    def _purge_selected_cache_file(self):
        pane = self.query_one(CachePane)
        table = pane.query_one("#cache-files-table")
        if table.cursor_row is None or table.row_count == 0:
            return
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        path = row_key.value
        if path and os.path.isfile(path):
            os.remove(path)
        pane.refresh_table()


def run():
    WorkbenchApp().run()
