"""Renders the dark basemap exactly ONCE and caches it as a big PNG.

The old plotter called ax.set_extent() inside the animation callback, which makes
Cartopy reproject every feature on every frame. Here Cartopy runs a single time at
~6000px wide; the animation then just crops and scales that raster, which is a
cv2.resize instead of a full geospatial redraw.
"""

import json
import os

import numpy as np
from PIL import Image

import config as C


def _meta_path(d):
    return os.path.join(d, "basemap.json")


def _png_path(d):
    return os.path.join(d, "basemap.png")


def _cities_path(d):
    return os.path.join(d, "cities.json")


def build(force=False):
    d = C.BASEMAP_CACHE
    os.makedirs(d, exist_ok=True)

    if os.path.exists(_meta_path(d)) and not force:
        with open(_meta_path(d)) as f:
            meta = json.load(f)
        if meta.get("extent") == C.REGION_EXTENT and meta.get("width") == C.BASEMAP_PX_WIDE:
            print("  basemap cache hit")
            return _load(d)

    print("Rendering basemap (one time, this is the slow part)...")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    import cartopy.io.shapereader as shpreader

    W, E, S, N = C.REGION_EXTENT
    bw = C.BASEMAP_PX_WIDE
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

    fig.canvas.draw()
    img = np.asarray(fig.canvas.buffer_rgba())[..., :3].copy()
    plt.close(fig)

    Image.fromarray(img).save(_png_path(d), optimize=False)

    # City labels are drawn per-frame at constant screen size, so they only need
    # coordinates here -- baking the text into the raster would make labels scale
    # with the zoom and become unreadable at both ends.
    cities = []
    try:
        shp = shpreader.natural_earth(resolution="10m", category="cultural",
                                      name="populated_places")
        for rec in shpreader.Reader(shp).records():
            a = rec.attributes
            pop = a.get("POP_MAX") or 0
            x, y = rec.geometry.x, rec.geometry.y
            if pop >= C.CITY_MIN_POP and W <= x <= E and S <= y <= N:
                cities.append({"name": a.get("NAME"), "lon": float(x),
                               "lat": float(y), "pop": int(pop)})
        cities.sort(key=lambda c: -c["pop"])
    except Exception as e:
        print(f"  skipped city labels: {e}")

    with open(_cities_path(d), "w") as fh:
        json.dump(cities, fh)
    with open(_meta_path(d), "w") as fh:
        json.dump({"extent": C.REGION_EXTENT, "width": bw, "height": bh}, fh)

    print(f"  basemap {bw}x{bh}, {len(cities)} city labels")
    return _load(d)


def _load(d):
    with open(_meta_path(d)) as f:
        meta = json.load(f)
    img = np.asarray(Image.open(_png_path(d)).convert("RGB"))
    cities = []
    if os.path.exists(_cities_path(d)):
        with open(_cities_path(d)) as f:
            cities = json.load(f)
    return img, meta, cities
