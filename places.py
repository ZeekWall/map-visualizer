"""Dispatches to All The Places (preferred) or Overpass (fallback/override) for
the brand POI list, per config.PLACE_SOURCE. All The Places scrapes brands'
own store-locator APIs weekly (CC0), so it doesn't inherit OSM's inconsistent
brand= tagging -- see README for the numbers that motivated this.
"""

import config as C
import progress
from atpapi import getPlaces as getPlacesATP
from overpassapi import getPlaces as getPlacesOSM


def _via_osm(refresh):
    places, cached = getPlacesOSM(C.PLACE_NAME, C.PLACE_MAIN_TYPE, C.PLACE_TYPE,
                                  C.REGION_NAME, refresh=refresh,
                                  queryClauses=C.PLACE_QUERY_CLAUSES)
    return places, cached, "osm"


def _via_static():
    places = [{"@id": name, "@lat": lat, "@lon": lon, "name": name, "brand": name}
             for name, lat, lon in C.STATIC_PLACES]
    return places, True, "static"


def _via_atp(refresh):
    places, cached = getPlacesATP(
        C.ATP_SPIDER, C.REGION_STATE, C.REGION_EXTENT,
        brands=C.ATP_BRANDS,
        include_instore=C.ATP_INCLUDE_INSTORE,
        refresh=refresh)
    return places, cached, "atp"


def get_places(refresh=False):
    """Returns (places, cached, source) where source is "static", "atp", or "osm"."""
    if C.STATIC_PLACES:
        return _via_static()

    source = C.PLACE_SOURCE

    if source == "osm":
        return _via_osm(refresh)

    if source == "atp":
        return _via_atp(refresh)

    # auto: try ATP when a spider is configured, fall back to Overpass on any
    # failure -- no spider, a network/HTTP error, or a result that filters
    # down to nothing (the failure mode most likely to ship a broken video).
    if C.ATP_SPIDER:
        try:
            places, cached, src = _via_atp(refresh)
            if places:
                return places, cached, src
            progress.warn("All The Places returned 0 rows after filtering, "
                          "falling back to Overpass")
        except Exception as e:
            progress.warn(f"All The Places fetch failed ({e}), falling back to Overpass")

    return _via_osm(refresh)
