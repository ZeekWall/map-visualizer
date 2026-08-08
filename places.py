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


def _via_atp(refresh):
    places, cached = getPlacesATP(
        C.ATP_SPIDER, getattr(C, "REGION_STATE", None), C.REGION_EXTENT,
        brands=getattr(C, "ATP_BRANDS", None),
        include_instore=getattr(C, "ATP_INCLUDE_INSTORE", True),
        refresh=refresh)
    return places, cached, "atp"


def get_places(refresh=False):
    """Returns (places, cached, source) where source is "atp" or "osm"."""
    source = getattr(C, "PLACE_SOURCE", "auto")

    if source == "osm":
        return _via_osm(refresh)

    if source == "atp":
        return _via_atp(refresh)

    # auto: try ATP when a spider is configured, fall back to Overpass on any
    # failure -- no spider, a network/HTTP error, or a result that filters
    # down to nothing (the failure mode most likely to ship a broken video).
    if getattr(C, "ATP_SPIDER", None):
        try:
            places, cached, src = _via_atp(refresh)
            if places:
                return places, cached, src
            progress.warn("All The Places returned 0 rows after filtering, "
                          "falling back to Overpass")
        except Exception as e:
            progress.warn(f"All The Places fetch failed ({e}), falling back to Overpass")

    return _via_osm(refresh)
