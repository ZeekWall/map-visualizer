import numpy as np
import matplotlib.pyplot as plt
import matplotlib.animation as animation
import cartopy.crs as ccrs
import cartopy.feature as cfeature
import cartopy.io.shapereader as shpreader
import geopandas as gpd
from overpassapi import getPlaces
from python_tsp.heuristics import solve_tsp_local_search

PLACE_NAME = "Costco"
PLACE_MAIN_TYPE = "shop"
PLACE_TYPE = "wholesale"

TX_EXTENT = [-106.7, -93.5, 25.5, 36.6]  # west, east, south, north


def haversine_distance_matrix(coords):
    R = 6371.0
    phi = np.radians(coords[:, 0])
    lambda_ = np.radians(coords[:, 1])
    dphi = phi[:, None] - phi
    dlambda = lambda_[:, None] - lambda_
    a = np.sin(dphi/2)**2 + np.cos(phi)[:, None] * np.cos(phi) * np.sin(dlambda/2)**2
    c = 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))
    return R * c

def nearest_neighbor_tour(dist_matrix):
    n = len(dist_matrix)
    unvisited = set(range(n))
    tour = [0]
    unvisited.remove(0)
    while unvisited:
        last = tour[-1]
        nxt = min(unvisited, key=lambda j: dist_matrix[last, j])
        tour.append(nxt)
        unvisited.remove(nxt)
    return tour

# ---- data + solve ----
places = getPlaces(PLACE_NAME, PLACE_MAIN_TYPE, PLACE_TYPE)
coords = np.array([[float(p["@lat"]), float(p["@lon"])] for p in places])
dist_matrix = haversine_distance_matrix(coords)
x0 = nearest_neighbor_tour(dist_matrix)
optimal_path, optimal_distance = solve_tsp_local_search(dist_matrix, x0=x0, max_processing_time=60)

ordered_indices = list(optimal_path) + [optimal_path[0]]
ordered_coords = coords[ordered_indices]
lats = ordered_coords[:, 0]
lons = ordered_coords[:, 1]

seg_dist = np.array([dist_matrix[ordered_indices[i], ordered_indices[i+1]]
                      for i in range(len(ordered_indices) - 1)])
cum_dist = np.concatenate([[0], np.cumsum(seg_dist)])
total_dist = cum_dist[-1]
NUM_SEGMENTS = len(seg_dist)

fig = plt.figure(figsize=(16, 9), dpi=120)
ax = plt.axes(projection=ccrs.PlateCarree())
ax.set_extent(TX_EXTENT, crs=ccrs.PlateCarree())
ax.add_feature(cfeature.LAND, facecolor='whitesmoke')
ax.add_feature(cfeature.OCEAN, facecolor='lightblue')
ax.add_feature(cfeature.STATES.with_scale('50m'), edgecolor='black', linewidth=0.5)

roads = cfeature.NaturalEarthFeature(
    category='cultural', name='roads_north_america', scale='10m',
    facecolor='none', edgecolor='dimgray'
)
ax.add_feature(roads, linewidth=0.4, zorder=2)

cities_shp = shpreader.natural_earth(resolution='10m', category='cultural', name='populated_places')
cities_df = gpd.read_file(cities_shp)
major_cities = cities_df[cities_df['POP_MAX'] > 500000]

BASE_CITY_FONTSIZE = 9
city_labels = []  # keep references so we can rescale fontsize as the camera zooms
for _, city in major_cities.iterrows():
    x, y = city.geometry.x, city.geometry.y
    if TX_EXTENT[0] <= x <= TX_EXTENT[1] and TX_EXTENT[2] <= y <= TX_EXTENT[3]:
        ax.plot(x, y, marker='.', color='black', markersize=4, transform=ccrs.PlateCarree(), zorder=4)
        label = ax.text(x + 0.1, y, city['NAME'], fontsize=BASE_CITY_FONTSIZE,
                          transform=ccrs.PlateCarree(), zorder=4)
        city_labels.append(label)

line, = ax.plot([], [], color='darkorange', linewidth=1.2, alpha=0.8, transform=ccrs.PlateCarree())
points = ax.scatter([], [], color='navy', s=8, zorder=5, transform=ccrs.PlateCarree())
start_marker = ax.plot(lons[0], lats[0], 'X', color='red', markersize=10,
                        transform=ccrs.PlateCarree(), zorder=6)

title_text = fig.text(0.5, 0.94, f"{PLACE_NAME} Road Trip", ha='center', fontsize=20, weight='bold')
stat_text = fig.text(0.5, 0.06, "", ha='center', fontsize=14)

DURATION_SEC = 20
FPS = 10
NUM_FRAMES = DURATION_SEC * FPS

FIG_ASPECT = 9 / 16
ZOOM_HALF_WIDTH_DEG = 1.2
# Reference zoom width the base fontsize/markersize were tuned for (the full state view).
# Labels/markers scale relative to this so they read correctly at the tighter follow-zoom.
REFERENCE_HALF_WIDTH_DEG = (TX_EXTENT[1] - TX_EXTENT[0]) / 2

def get_camera_extent(cur_lon, cur_lat):
    # No wide intro shot — start already zoomed in on the route from frame 0.
    half_w = ZOOM_HALF_WIDTH_DEG
    half_h = half_w * FIG_ASPECT
    return [cur_lon - half_w, cur_lon + half_w, cur_lat - half_h, cur_lat + half_h]

def update(frame):
    progress = frame / (NUM_FRAMES - 1)

    scaled = progress * NUM_SEGMENTS
    seg_idx = min(int(scaled), NUM_SEGMENTS - 1)
    frac = scaled - seg_idx

    cur_lon = lons[seg_idx] + frac * (lons[seg_idx + 1] - lons[seg_idx])
    cur_lat = lats[seg_idx] + frac * (lats[seg_idx + 1] - lats[seg_idx])

    visible_lons = np.append(lons[:seg_idx + 1], cur_lon)
    visible_lats = np.append(lats[:seg_idx + 1], cur_lat)
    line.set_data(visible_lons, visible_lats)
    points.set_offsets(np.column_stack([lons[:seg_idx + 1], lats[:seg_idx + 1]]))

    ax.set_extent(get_camera_extent(cur_lon, cur_lat), crs=ccrs.PlateCarree())

    # Scale city label fontsize so it reads correctly at this zoom level
    # instead of staying fixed at the wide-shot size.
    scale_factor = REFERENCE_HALF_WIDTH_DEG / ZOOM_HALF_WIDTH_DEG
    for label in city_labels:
        label.set_fontsize(BASE_CITY_FONTSIZE * min(scale_factor, 2.5))  # cap growth so it doesn't get huge

    dist_so_far = cum_dist[seg_idx] + frac * seg_dist[seg_idx]
    stat_text.set_text(f"{seg_idx + 1}/{NUM_SEGMENTS} stops | "
                        f"{dist_so_far:,.0f} / {total_dist:,.0f} km")
    return line, points, stat_text, *city_labels

ani = animation.FuncAnimation(fig, update, frames=NUM_FRAMES, blit=False, interval=1000/FPS)

print("Creating animation")
writer = animation.FFMpegWriter(fps=FPS, bitrate=2000)
ani.save(f'{PLACE_NAME}_route.mp4', writer=writer)
print(f"Saved {PLACE_NAME}.mp4")