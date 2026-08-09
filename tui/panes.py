"""Widgets for each tab of the TUI. app.py owns navigation/orchestration;
these own their own layout and (where it makes sense) their own event
handling, updated by app.py pushing data in rather than reaching into them.
"""

import importlib
import os
import time

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (Button, Collapsible, DataTable, Input, Label,
                             ProgressBar, Select, Static, Switch)

import config
import knobs
import targets
import targets_edit
import textnorm

# Every USPS state code + DC that Natural Earth's admin-1 shapefile carries
# (targets.resolve_region looks any of these up automatically; AK/HI are
# pre-seeded overrides in targets.REGIONS for the dateline/island cases).
US_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY",
]

# Display labels for the 6 pipeline stages, 1-indexed to match progress.py's
# step(i, n, name) -- stage 6 is reported as "Render" or "Cover" depending on
# --cover-only, so rows are keyed by index, not by name.
STAGE_COUNT = 6
STAGE_LABELS = {1: "Places", 2: "Route", 3: "Roads", 4: "Basemap", 5: "Camera", 6: "Render/Cover"}


class RunPane(VerticalScroll):
    """Target/region/output pickers, the 6-stage progress list, and the
    render/cover/cancel controls. Doesn't call into places.py/main.py
    itself -- app.py owns the worker and pushes step/done/warn/bar events
    in via update_stage()/set_bar()/log_line(), so this widget has no idea
    the pipeline even runs on a thread.

    VerticalScroll (not Vertical) as a deliberate safety net: with every row
    height fixed correctly this fits an ordinary terminal without scrolling,
    but a narrow/short terminal or future rows added here should degrade to
    "scroll for it" rather than "silently clipped, unreachable" -- which is
    exactly how the progress bar and stage list went missing before (see the
    comment in compose() below).
    """

    def compose(self) -> ComposeResult:
        # Every row here is an explicit fixed/auto height (see the CSS in
        # app.py) -- Textual's Horizontal defaults to `height: 1fr`, which
        # made these rows greedily fill the tab's entire remaining space and
        # pushed the stage list/progress bar/status line below the visible
        # viewport with no scrollbar to reach them. That was the actual bug
        # behind "there's no progress indication": it was rendering, just
        # off-screen.
        with Horizontal(id="run-pickers"):
            yield Select(
                [(f"{k} -- {t.name}", k) for k, t in sorted(targets.TARGETS.items())],
                value="heb" if "heb" in targets.TARGETS else None,
                id="target-select", allow_blank=False,
            )
            yield Select(
                [(s, s) for s in US_STATES], value="TX",
                id="region-select", allow_blank=False,
            )
        with Horizontal(id="run-options"):
            yield Label("seconds")
            yield Input(value="61", id="duration-input", type="integer")
            yield Label("res")
            yield Select([("1080", 1080), ("1440", 1440), ("2160", 2160)],
                        value=1440, id="res-select", allow_blank=False)
            yield Label("preview")
            yield Switch(id="preview-switch")
        with Horizontal(id="run-buttons"):
            yield Button("Render", id="render-btn", variant="primary")
            yield Button("Cover only", id="cover-btn")
            yield Button("Cancel", id="cancel-btn", disabled=True, variant="error")

        with Vertical(id="stages"):
            for i in range(1, STAGE_COUNT + 1):
                yield Static(f"  {STAGE_LABELS[i]}", id=f"stage-{i}", classes="stage-idle")
        with Horizontal(id="render-bar-row"):
            yield ProgressBar(id="render-bar", show_eta=True)
            yield Static("", id="render-bar-count")
        yield Static("", id="run-status")

    def set_stage(self, i, name, state, detail=""):
        """state: 'idle' | 'active' | 'done' | 'warn'. `i` is the 1-indexed
        step number from progress.py's step(i, n, name) -- stage 6's `name`
        varies ("Render" vs "Cover"), so rows are addressed by index and
        just display whatever name actually arrived."""
        if i is None:
            return
        try:
            w = self.query_one(f"#stage-{i}", Static)
        except Exception:
            return
        icon = {"idle": "  ", "active": "> ", "done": "✓ ", "warn": "! "}[state]
        label = name or STAGE_LABELS.get(i, "")
        w.update(f"{icon}{label}  {detail}")
        w.set_classes(f"stage-{state}")

    def reset_stages(self):
        for i in range(1, STAGE_COUNT + 1):
            self.set_stage(i, STAGE_LABELS[i], "idle")
        self.query_one("#render-bar", ProgressBar).update(total=100, progress=0)
        self.query_one("#render-bar-count", Static).update("")

    def set_bar(self, n, total, unit=""):
        bar = self.query_one("#render-bar", ProgressBar)
        bar.update(total=total, progress=n)
        # ProgressBar itself only shows percentage/ETA -- the frame count
        # (e.g. "812 / 1,830 frames") is otherwise invisible, and for a
        # render that's the number that actually tells you how far along
        # a multi-minute encode really is.
        self.query_one("#render-bar-count", Static).update(f"{n:,}/{total:,}{unit}")

    def stop_bar(self):
        """Freeze the bar after a cancel/failure. ProgressBar's own
        percentage/ETA sub-widgets recompute against wall-clock time on
        their own refresh timer -- elapsed keeps growing even once nothing
        is left updating progress/total, so without this the bar (and its
        ETA text) visibly keeps ticking forever after a cancelled render.
        Reset to the same static 0% state reset_stages() starts from --
        NOT total=None, which ProgressBar renders as an indeterminate
        pulsing animation and would make it look even more "still going".
        """
        bar = self.query_one("#render-bar", ProgressBar)
        bar.update(total=100, progress=0)
        self.query_one("#render-bar-count", Static).update("")

    def set_running(self, running):
        self.query_one("#render-btn", Button).disabled = running
        self.query_one("#cover-btn", Button).disabled = running
        self.query_one("#cancel-btn", Button).disabled = not running

    def set_blocked(self, blocked):
        """Like set_running(), minus the Cancel button -- for when Render/
        Cover need to be disabled because something else that shares
        progress.py's global state (an Explore tab fetch) is running, but
        there's no cancel event wired up to make Cancel meaningful."""
        self.query_one("#render-btn", Button).disabled = blocked
        self.query_one("#cover-btn", Button).disabled = blocked

    def status(self, text):
        self.query_one("#run-status", Static).update(text)


def _knob_row(k: knobs.Knob):
    """One Label + editable control for a single knob. The control's id is
    always `knob-<NAME>` so the pane's event handlers can map a Textual
    event straight back to knobs.write_value(name, ...) without keeping a
    separate lookup table in sync."""
    label = k.name if not k.comment else f"{k.name}  [dim]({k.comment})[/]"
    if k.kind == "bool":
        control = Switch(value=k.value, id=f"knob-{k.name}")
    else:
        # color/list/other are edited as their repr and re-parsed with
        # ast.literal_eval on submit (see KnobsPane.on_input_submitted) --
        # simplest thing that can represent a 3-tuple or a list without a
        # bespoke widget per shape.
        text = repr(k.value) if k.kind in ("color", "list", "other") else str(k.value)
        control = Input(value=text, id=f"knob-{k.name}")
    return Horizontal(Label(label, classes="knob-label"), control, classes="knob-row")


class KnobsPane(VerticalScroll):
    """Every 'how it looks' knob, grouped exactly as config.py groups them
    (knobs.grouped() -- see that module for why the grouping can't drift
    from the file). Each row saves itself on submit/toggle rather than
    needing a global Save -- fewer surprises about what did or didn't get
    written, and knobs.write_value() already validates+rolls back on its
    own, so a bad edit just gets rejected in place with a status line.
    """

    def compose(self) -> ComposeResult:
        for section, group in knobs.grouped():
            title = section or "misc"
            with Collapsible(title=title, collapsed=(title not in ("target", "video"))):
                for k in group:
                    yield _knob_row(k)
        yield Static("", id="knobs-status")

    def _write(self, name, raw_value, kind):
        try:
            if kind in ("color", "list", "other"):
                import ast
                value = ast.literal_eval(raw_value)
            elif kind == "int":
                value = int(raw_value)
            elif kind == "float":
                value = float(raw_value)
            elif kind == "none":
                value = None if raw_value.strip() in ("", "None") else raw_value
            else:
                value = raw_value
            knobs.write_value(name, value)
            # write_value only touches config.py on disk -- every module
            # (including tui/pipeline.py, which renders in-process) already
            # holds a reference to the one `config` module object from its
            # own `import config as C` at startup, so without this the
            # edited value is invisible until the TUI restarts.
            importlib.reload(config)
            self.status(f"saved {name} = {value!r}")
        except (ValueError, SyntaxError) as e:
            self.status(f"[red]rejected {name}: {e}[/]")

    def on_input_submitted(self, event: Input.Submitted):
        if not event.input.id or not event.input.id.startswith("knob-"):
            return
        name = event.input.id[len("knob-"):]
        k = next((kk for kk in knobs.scan() if kk.name == name), None)
        if k is None:
            return
        self._write(name, event.value, k.kind)

    def on_switch_changed(self, event: Switch.Changed):
        if not event.switch.id or not event.switch.id.startswith("knob-"):
            return
        name = event.switch.id[len("knob-"):]
        self._write(name, event.value, "bool")

    def status(self, text):
        try:
            self.query_one("#knobs-status", Static).update(text)
        except Exception:
            pass


class OutputPane(Vertical):
    """List of past renders under out/."""

    def compose(self) -> ComposeResult:
        table = DataTable(id="renders-table")
        table.add_columns("folder", "file", "size", "modified")
        yield table
        yield Button("Refresh", id="refresh-outputs-btn")

    def refresh_table(self, out_dir="out"):
        table = self.query_one("#renders-table", DataTable)
        table.clear()
        if not os.path.isdir(out_dir):
            return
        for brand_dir in sorted(os.listdir(out_dir)):
            full = os.path.join(out_dir, brand_dir)
            if not os.path.isdir(full):
                continue
            for fn in sorted(os.listdir(full)):
                p = os.path.join(full, fn)
                st = os.stat(p)
                size = f"{st.st_size / 1e6:.1f} MB" if st.st_size > 1e6 else f"{st.st_size / 1e3:.0f} KB"
                mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
                table.add_row(brand_dir, fn, size, mtime, key=p)


class ExplorePane(Vertical):
    """Brand discovery: type a name, see how many OSM/ATP locations exist
    and under what tags, sample a few, then Add writes a Target() entry
    into targets.py. explore.py/targets_edit.py own the actual lookup and
    write logic (same split as knobs.py backing KnobsPane) -- this widget
    only displays results, collects the Add form, and reacts to row
    selection to prefill it. app.py owns the worker thread the network
    calls (Overpass, GitHub, All The Places) run on, same split as
    RunPane/the pipeline, since both this pane and the pipeline share
    progress.py's process-global sink/_TTY and must never run at once.

    The status line is docked at the top, outside the scrollable body --
    with everything else in a VerticalScroll, a plain trailing Static (the
    RunPane/KnobsPane pattern) scrolls out of view the moment there's
    enough content to scroll at all, which defeats its purpose here since a
    search/probe/add result needs to stay visible while the user scrolls
    down to the table it refers to.
    """

    def __init__(self):
        super().__init__()
        self._osm_probe = None
        self._atp_probe = None
        self._region_name = None
        self._region_state = None
        self._region_extent = None
        self._selected_brands = set()  # source of truth for #add-brands;
                                       # toggled by Enter/click on brand-table
        self._pending_overwrite_slug = None  # set by a rejected duplicate add;
                                             # a second Add click with the same
                                             # slug confirms the overwrite

    def compose(self) -> ComposeResult:
        yield Static("", id="explore-status")
        with VerticalScroll(id="explore-scroll"):
            with Horizontal(id="explore-search"):
                yield Input(placeholder="brand name, e.g. Trader Joe's", id="explore-query")
                yield Select([(s, s) for s in US_STATES], value="TX",
                            id="explore-region", allow_blank=False)
                yield Button("Search", id="explore-search-btn", variant="primary")
                yield Button("Cancel", id="explore-cancel-btn", disabled=True, variant="error")

            yield Label("OSM matches by type")
            osm_table = DataTable(id="osm-table", cursor_type="row")
            osm_table.add_columns("main", "type", "count")
            yield osm_table

            yield Label("Brand tag values (Enter/click to toggle ☑ -- multi-select feeds "
                       "atp_brands; — rows can't be selected)")
            brand_table = DataTable(id="brand-table", cursor_type="row")
            brand_table.add_columns(("", "sel"), "brand", "count")
            yield brand_table

            with Horizontal(id="atp-row"):
                yield Label("All The Places spider candidates")
                yield Button("Probe selected", id="atp-probe-btn")
            atp_table = DataTable(id="atp-table", cursor_type="row")
            atp_table.add_columns("spider", ("probed count", "count"))
            yield atp_table

            yield Label("Sample rows")
            sample_table = DataTable(id="explore-sample", cursor_type="row")
            sample_table.add_columns("name", "brand", "lat", "lon")
            yield sample_table

            with Collapsible(title="Add to targets.py", collapsed=False, id="add-collapsible"):
                with Horizontal(classes="add-row"):
                    yield Label("slug", classes="add-label")
                    yield Input(id="add-slug")
                    yield Label("name", classes="add-label")
                    yield Input(id="add-name")
                with Horizontal(classes="add-row"):
                    yield Label("osm main", classes="add-label")
                    yield Input(id="add-main", placeholder="shop / amenity")
                    yield Label("osm type", classes="add-label")
                    yield Input(id="add-type", placeholder="supermarket / fast_food")
                with Horizontal(classes="add-row"):
                    yield Label("atp spider", classes="add-label")
                    yield Input(id="add-spider")
                with Horizontal(classes="add-row"):
                    yield Label("brands (comma-separated)", classes="add-label")
                    yield Input(id="add-brands")
                with Horizontal(classes="add-row"):
                    yield Label("include in-store", classes="add-label")
                    yield Switch(id="add-instore")
                    yield Label("generate osm_clauses from brands", classes="add-label")
                    yield Switch(id="add-clauses")
                yield Button("Add target", id="add-target-btn", variant="success")

    def set_busy(self, busy):
        self.query_one("#explore-search-btn", Button).disabled = busy
        self.query_one("#atp-probe-btn", Button).disabled = busy
        self.query_one("#add-target-btn", Button).disabled = busy
        self.query_one("#explore-cancel-btn", Button).disabled = not busy

    def status(self, text):
        self.query_one("#explore-status", Static).update(text)

    # ---- results wiring --------------------------------------------------

    def show_osm_result(self, probe):
        self._osm_probe = probe
        table = self.query_one("#osm-table", DataTable)
        table.clear()
        for main, typ, count in probe.by_type:
            table.add_row(main or "(none)", typ or "(none)", str(count), key=f"{main}|{typ}")

        self._fill_brand_table(probe.by_brand)
        self._fill_sample(probe.rows)

        query = self.query_one("#explore-query", Input).value.strip()
        slug_input = self.query_one("#add-slug", Input)
        if not slug_input.value:
            slug_input.value = targets_edit.slugify(query)
        name_input = self.query_one("#add-name", Input)
        if not name_input.value:
            # Prefer the actual capitalization OSM carries (a "sonic"
            # search should fill in "Sonic", not the lowercase query text)
            # -- probe.by_brand is tallied over the full result set, not
            # just the 20-row sample, so it's the more reliable source.
            best = self._best_brand(probe.by_brand) or self._dominant_name(probe.rows)
            name_input.value = best or query
        if probe.by_type and not self.query_one("#add-main", Input).value:
            main, typ, _ = probe.by_type[0]
            self.query_one("#add-main", Input).value = main
            self.query_one("#add-type", Input).value = typ

    def show_spider_candidates(self, names):
        table = self.query_one("#atp-table", DataTable)
        table.clear()
        for name in names:
            table.add_row(name, "", key=name)

    def show_atp_probe(self, probe):
        self._atp_probe = probe
        table = self.query_one("#atp-table", DataTable)
        try:
            table.update_cell(probe.spider, "count", str(probe.total))
        except Exception:
            table.add_row(probe.spider, str(probe.total), key=probe.spider)

        self._fill_brand_table(probe.by_brand)
        self._fill_sample(probe.rows)
        spider_input = self.query_one("#add-spider", Input)
        if not spider_input.value:
            spider_input.value = probe.spider
        name_input = self.query_one("#add-name", Input)
        if not name_input.value:
            best = self._best_brand(probe.by_brand) or self._dominant_name(probe.rows)
            if best:
                name_input.value = best

    _MARK_UNCHECKED = Text("☐", style="dim")       # ☐
    _MARK_CHECKED = Text("☑", style="bold green")  # ☑
    _MARK_NA = Text("—", style="dim italic")        # em dash -- not selectable

    def _fill_brand_table(self, by_brand):
        """(Re)populate the brand-tag table for a fresh probe. Selection
        state doesn't carry across a new search/probe -- the brand set
        being shown has changed, so a stale checkmark would misrepresent
        what's actually going into atp_brands.

        The "(untagged)" bucket (brand == "") gets a dash instead of a
        checkbox -- it's not a real `brand=` value, so it can never be
        added to atp_brands. Giving it the same empty-checkbox look as a
        real, currently-unselected row was the actual bug report: visually
        indistinguishable from a selectable row that DataTable's own
        cursor highlight was sitting on, so it looked "stuck selected" even
        though it was never in self._selected_brands at all.
        """
        self._selected_brands = set()
        self.query_one("#add-brands", Input).value = ""
        table = self.query_one("#brand-table", DataTable)
        table.clear()
        for brand, count in by_brand:
            mark = self._MARK_NA if not brand else self._MARK_UNCHECKED
            table.add_row(mark, brand or "(untagged)", str(count), key=brand)

    def _fill_sample(self, rows):
        table = self.query_one("#explore-sample", DataTable)
        table.clear()
        for i, r in enumerate(rows):
            table.add_row(r.get("name", ""), r.get("brand", ""),
                         str(r.get("@lat", "")), str(r.get("@lon", "")), key=str(i))

    @staticmethod
    def _best_brand(by_brand):
        """First non-empty brand= value from a (brand, count) list already
        sorted by count descending -- the tag OSM chain mapping intends for
        canonical capitalization ("Sonic"), so it beats a raw name= value
        or the user's own (possibly lowercase) search text."""
        for brand, _count in by_brand:
            if brand:
                return brand
        return None

    @staticmethod
    def _dominant_name(rows):
        """Most common non-empty name= value among a set of raw rows --
        fallback for when nothing carries a brand= tag at all."""
        counts = {}
        for r in rows:
            name = (r.get("name") or "").strip()
            if name:
                counts[name] = counts.get(name, 0) + 1
        return max(counts.items(), key=lambda kv: kv[1])[0] if counts else None

    # ---- selection prefills the Add form ---------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        table_id = event.data_table.id
        key = event.row_key.value

        if table_id == "osm-table" and key is not None:
            main, _, typ = key.partition("|")
            self.query_one("#add-main", Input).value = main
            self.query_one("#add-type", Input).value = typ
            if self._osm_probe:
                filtered = [r for r in self._osm_probe.rows
                           if (r.get("amenity") or r.get("shop") or "") == typ]
                rows_for_type = filtered or self._osm_probe.rows
                self._fill_sample(rows_for_type)
                # Picking a specific type is an explicit "use this" action
                # (same as add-main/add-type above), so it overwrites
                # add-name too -- e.g. narrowing a "sonic" search down to
                # just amenity=fast_food should still read "Sonic", not
                # whatever the untyped/mixed-type default landed on.
                best = self._dominant_name(rows_for_type)
                if best:
                    self.query_one("#add-name", Input).value = best

        elif table_id == "brand-table" and key is not None:
            if not key:
                self.status("(untagged) has no brand= value -- can't be added to atp_brands")
                return
            if key in self._selected_brands:
                self._selected_brands.discard(key)
                mark = self._MARK_UNCHECKED
            else:
                self._selected_brands.add(key)
                mark = self._MARK_CHECKED
            event.data_table.update_cell(key, "sel", mark)
            self.query_one("#add-brands", Input).value = ", ".join(sorted(self._selected_brands))

        elif table_id == "atp-table" and key:
            self.query_one("#add-spider", Input).value = key

    # ---- Add ---------------------------------------------------------------

    def do_add_target(self):
        """Validate the form and write the Target() entry. Returns the new
        slug on success (so app.py knows to refresh the Run tab's target
        dropdown), or None if the add was rejected -- status() already
        carries the reason either way."""
        slug = self.query_one("#add-slug", Input).value.strip()
        name = self.query_one("#add-name", Input).value.strip()
        main = self.query_one("#add-main", Input).value.strip()
        typ = self.query_one("#add-type", Input).value.strip()
        spider = self.query_one("#add-spider", Input).value.strip() or None
        brands_raw = self.query_one("#add-brands", Input).value.strip()
        brands = {b.strip() for b in brands_raw.split(",") if b.strip()} or None
        include_instore = self.query_one("#add-instore", Switch).value
        gen_clauses = self.query_one("#add-clauses", Switch).value

        if not slug or not name or not main or not typ:
            self.status("[red]slug, name, osm main, and osm type are all required[/]")
            return None

        osm_clauses = None
        if gen_clauses:
            if not brands:
                self.status("[red]osm_clauses needs at least one brand value[/]")
                return None
            # Regex match with fuzzy_regex, not an exact "=", for the same
            # reason the hand-written heb entry does -- OSM's brand= tag
            # for a store can carry a different apostrophe/dash than
            # whatever spelling ended up in this set (see textnorm.py).
            osm_clauses = tuple(
                f'nwr["{main}"="{typ}"]["brand"~"^{textnorm.fuzzy_regex(b)}$",i](area.region);'
                for b in sorted(brands))

        # Second click on the same slug after the "already exists" warning
        # below confirms the overwrite -- no modal dialog, just "do it
        # again to mean it," consistent with there being no other
        # confirmation UI anywhere else in this app.
        overwrite = slug == self._pending_overwrite_slug
        try:
            targets_edit.add_target(
                slug, name, (main, typ), atp_spider=spider, atp_brands=brands,
                include_instore=include_instore, osm_clauses=osm_clauses,
                overwrite=overwrite)
        except targets_edit.TargetExistsError:
            self._pending_overwrite_slug = slug
            self.status(f"[yellow]{slug!r} already exists in targets.py -- "
                       f"click Add target again to overwrite it[/]")
            return None
        except ValueError as e:
            self._pending_overwrite_slug = None
            self.status(f"[red]{e}[/]")
            return None

        self._pending_overwrite_slug = None
        verb = "overwrote" if overwrite else "added"
        self.status(f"{verb} {slug!r} in targets.py -- pick it up on the Run tab")
        return slug


class CachePane(Vertical):
    """cache/{places,basemap,route,roads,explore} plus regions.json --
    entry counts, total size, newest file's mtime per category, a
    whole-category purge, and (select a category row to drill in) a listing
    of that category's individual files with their own purge -- e.g. one
    stale Overpass query for a single brand/region without wiping every
    other cached place lookup alongside it."""

    DIRS = ["places", "basemap", "route", "roads", "explore"]

    def __init__(self):
        super().__init__()
        self._current_dir = None  # category path currently drilled into

    def compose(self) -> ComposeResult:
        table = DataTable(id="cache-table", cursor_type="row")
        table.add_columns("cache", "files", "size", "newest")
        yield table
        with Horizontal(id="cache-buttons"):
            yield Button("Refresh", id="refresh-cache-btn")
            yield Button("Purge selected", id="purge-cache-btn", variant="error")

        yield Label("Select a category above to see its individual files", id="cache-files-label")
        files_table = DataTable(id="cache-files-table", cursor_type="row")
        files_table.add_columns("file", "size", "modified")
        yield files_table
        yield Button("Purge selected file", id="purge-cache-file-btn", variant="error")

    def _dir_stats(self, path):
        if not os.path.isdir(path):
            return 0, 0, None
        n, total, newest = 0, 0, None
        for fn in os.listdir(path):
            p = os.path.join(path, fn)
            if not os.path.isfile(p):
                continue
            st = os.stat(p)
            n += 1
            total += st.st_size
            newest = max(newest or 0, st.st_mtime)
        return n, total, newest

    def refresh_table(self):
        table = self.query_one("#cache-table", DataTable)
        table.clear()
        for name in self.DIRS:
            path = os.path.join("cache", name)
            n, total, newest = self._dir_stats(path)
            size = f"{total / 1e6:.1f} MB" if total > 1e6 else f"{total / 1e3:.0f} KB"
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(newest)) if newest else "--"
            table.add_row(name, str(n), size, when, key=path)
        # Keep an already-open drill-down in sync -- e.g. after a category
        # purge or a single-file purge, its row count/size just changed.
        if self._current_dir is not None:
            self.show_files(self._current_dir)

    def show_files(self, path):
        """Drill into one cache category: list its individual files so a
        single stale entry can be purged without clearing the whole
        category."""
        self._current_dir = path
        name = os.path.basename(path)
        label = self.query_one("#cache-files-label", Label)
        table = self.query_one("#cache-files-table", DataTable)
        table.clear()
        if not os.path.isdir(path):
            label.update(f"{name}/ -- no directory yet")
            return
        files = sorted(fn for fn in os.listdir(path) if os.path.isfile(os.path.join(path, fn)))
        for fn in files:
            p = os.path.join(path, fn)
            st = os.stat(p)
            size = f"{st.st_size / 1e6:.1f} MB" if st.st_size > 1e6 else f"{st.st_size / 1e3:.0f} KB"
            mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
            table.add_row(fn, size, mtime, key=p)
        label.update(f"{name}/ -- {len(files)} file(s)" if files else f"{name}/ -- empty")

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        if event.data_table.id == "cache-table" and event.row_key.value:
            self.show_files(event.row_key.value)
