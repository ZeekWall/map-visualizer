"""Renders the dark basemap exactly ONCE and caches it as a big PNG.

The old plotter called ax.set_extent() inside the animation callback, which makes
Cartopy reproject every feature on every frame. Here Cartopy runs a single time at
BASEMAP_PX_WIDE (scaled to the output resolution); the animation then just crops
and scales that raster, which is a cv2.resize instead of a full geospatial redraw.
"""

import json
import os

import numpy as np
from PIL import Image

import config as C
import progress


def _meta_path(d, bw):
    return os.path.join(d, f"basemap_{bw}.json")


def _png_path(d, bw):
    return os.path.join(d, f"basemap_{bw}.png")


def _cities_path(d):
    return os.path.join(d, "cities.json")


def _scan_cities(extent, min_pop):
    """Populated places inside extent, sorted biggest first."""
    import cartopy.io.shapereader as shpreader

    W, E, S, N = extent
    cities = []
    shp = shpreader.natural_earth(resolution="10m", category="cultural",
                                  name="populated_places")
    for rec in shpreader.Reader(shp).records():
        a = rec.attributes
        pop = a.get("POP_MAX") or 0
        x, y = rec.geometry.x, rec.geometry.y
        if pop >= min_pop and W <= x <= E and S <= y <= N:
            cities.append({"name": a.get("NAME"), "lon": float(x),
                           "lat": float(y), "pop": int(pop)})
    cities.sort(key=lambda c: -c["pop"])
    return cities


def build(force=False):
    d = C.BASEMAP_CACHE
    os.makedirs(d, exist_ok=True)

    # BASEMAP_PX_WIDE is authored per 1080p of output width; scale it with
    # OUT_W so a 1440/2160 render gets a correspondingly sharper raster
    # instead of just upscaling the same 6000px source further. Cached per
    # width so switching --res doesn't force a rebuild every time.
    bw = int(round(C.BASEMAP_PX_WIDE * C.OUT_W / 1080))

    if os.path.exists(_meta_path(d, bw)) and not force:
        with open(_meta_path(d, bw)) as f:
            meta = json.load(f)
        if meta.get("extent") == C.REGION_EXTENT and meta.get("width") == bw:
            if meta.get("city_min_pop") != C.CITY_MIN_POP:
                # raster is still valid; only the label pool changed, and
                # rescanning the shapefile is seconds vs. minutes for a
                # full Cartopy rebuild
                with progress.spinner("rescanning populated places..."):
                    cities = _scan_cities(C.REGION_EXTENT, C.CITY_MIN_POP)
                with open(_cities_path(d), "w") as fh:
                    json.dump(cities, fh)
                meta["city_min_pop"] = C.CITY_MIN_POP
                with open(_meta_path(d, bw), "w") as fh:
                    json.dump(meta, fh)
                progress.done(f"{len(cities)} city labels (rescanned)")
            else:
                progress.done(f"{meta['width']}x{meta['height']} (cached)")
            return _load(d, bw)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature

    W, E, S, N = C.REGION_EXTENT
    bh = int(round(bw * (N - S) / (E - W)))

    dpi = 100
    fig = plt.figure(figsize=(bw / dpi, bh / dpi), dpi=dpi)
    ax = fig.add_axes([0, 0, 1, 1], projection=ccrs.PlateCarree())
    ax.set_extent(C.REGION_EXTENT, crs=ccrs.PlateCarree())
    ax.set_axis_off()

    f = lambda c: tuple(v / 255 for v in c)
    fig.patch.set_facecolor(f(C.BG))
    ax.set_facecolor(f(C.WATER))

    ax.add_feature(cfeature.OCEAN.with_scale("50m"), facecolor=f(C.WATER), zorder=0)
    ax.add_feature(cfeature.LAND.with_scale("50m"), facecolor=f(C.LAND), zorder=1)

    for name, scale in (("lakes", "10m"),):
        try:
            ax.add_feature(
                cfeature.NaturalEarthFeature("physical", name, scale),
                facecolor=f(C.WATER), edgecolor="none", zorder=2,
            )
        except Exception as e:
            print(f"  skipped {name}: {e}")

    if C.DRAW_ROADS:
        try:
            roads = cfeature.NaturalEarthFeature(
                "cultural", "roads_north_america", "10m",
                facecolor="none", edgecolor=f(C.ROAD),
            )
            ax.add_feature(roads, linewidth=bw / 2400, zorder=3)
        except Exception as e:
            print(f"  skipped roads: {e}")

    ax.add_feature(
        cfeature.STATES.with_scale("50m"),
        edgecolor=f(C.LAND_EDGE), facecolor="none", linewidth=bw / 1600, zorder=4,
    )
    ax.add_feature(
        cfeature.BORDERS.with_scale("50m"),
        edgecolor=f(C.LAND_EDGE), facecolor="none", linewidth=bw / 1200, zorder=4,
    )
    ax.add_feature(
        cfeature.COASTLINE.with_scale("50m"),
        edgecolor=f(C.LAND_EDGE), linewidth=bw / 1600, zorder=4,
    )

    with progress.spinner("rendering (first run also fetches Natural Earth data)..."):
        fig.canvas.draw()
        img = np.asarray(fig.canvas.buffer_rgba())[..., :3].copy()
        plt.close(fig)

    with progress.spinner("saving raster..."):
        Image.fromarray(img).save(_png_path(d, bw), optimize=False)

    # City labels are drawn per-frame at constant screen size, so they only need
    # coordinates here -- baking the text into the raster would make labels scale
    # with the zoom and become unreadable at both ends.
    cities = []
    with progress.spinner("scanning populated places..."):
        try:
            cities = _scan_cities(C.REGION_EXTENT, C.CITY_MIN_POP)
        except Exception as e:
            print(f"  skipped city labels: {e}")

    with open(_cities_path(d), "w") as fh:
        json.dump(cities, fh)
    with open(_meta_path(d, bw), "w") as fh:
        json.dump({"extent": C.REGION_EXTENT, "width": bw, "height": bh,
                   "city_min_pop": C.CITY_MIN_POP}, fh)

    progress.done(f"{bw}x{bh}, {len(cities)} city labels")
    return _load(d, bw)


def _load(d, bw):
    with open(_meta_path(d, bw)) as f:
        meta = json.load(f)
    img = np.asarray(Image.open(_png_path(d, bw)).convert("RGB"))
    cities = []
    if os.path.exists(_cities_path(d)):
        with open(_cities_path(d)) as f:
            cities = json.load(f)
    return img, meta, cities
