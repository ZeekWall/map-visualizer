#!/usr/bin/env bash
# One-time setup for road-routed legs: downloads a regional OSM extract,
# builds an OSRM graph, and serves it on localhost:5000.
#
# Usage: scripts/osrm-setup.sh <geofabrik-path> [region-name]
#   scripts/osrm-setup.sh north-america/us/texas
#   scripts/osrm-setup.sh north-america/us/california california
#
# The extract must cover config.py's REGION_NAME/REGION_EXTENT. Switching
# regions means re-running this script with a new extract; OSRM only serves
# whatever graph you built.
#
# Requires Docker. See https://download.geofabrik.de for available extracts.

set -euo pipefail

GEOFABRIK_PATH="${1:?Usage: $0 <geofabrik-path> [region-name]}"
REGION_NAME="${2:-$(basename "$GEOFABRIK_PATH")}"
OSRM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/osrm"
PBF="${REGION_NAME}-latest.osm.pbf"
GRAPH="${REGION_NAME}-latest.osrm"

mkdir -p "$OSRM_DIR"
cd "$OSRM_DIR"

if [ ! -f "$PBF" ]; then
  echo "Downloading ${GEOFABRIK_PATH}-latest.osm.pbf ..."
  curl -fSL -o "$PBF" "https://download.geofabrik.de/${GEOFABRIK_PATH}-latest.osm.pbf"
fi

if [ ! -f "$GRAPH" ]; then
  echo "Extracting (car profile) ..."
  docker run -t --rm -v "$OSRM_DIR:/data" osrm/osrm-backend osrm-extract -p /opt/car.lua "/data/${PBF}"
  echo "Partitioning ..."
  docker run -t --rm -v "$OSRM_DIR:/data" osrm/osrm-backend osrm-partition "/data/${GRAPH}"
  echo "Customizing ..."
  docker run -t --rm -v "$OSRM_DIR:/data" osrm/osrm-backend osrm-customize "/data/${GRAPH}"
fi

echo "Starting OSRM on http://localhost:5000 (Ctrl-C to stop) ..."
docker run -t --rm -p 5000:5000 -v "$OSRM_DIR:/data" osrm/osrm-backend \
  osrm-routed --algorithm mld "/data/${GRAPH}"
