import requests, io, csv, os

CACHE_FILE = "places.csv"

def getPlaces(placeName, placeMainType, placeType):
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f, delimiter='\t'))

    api_url = "https://overpass-api.de/api/interpreter"
    query = f"""
    [out:csv(::id,::lat,::lon,name,brand)][timeout:180];
    area["name"="Texas"]["boundary"="administrative"]["admin_level"="4"]->.tx;
    nwr["brand"="{placeName}"]["{placeMainType}"="{placeType}"]["name"!="Future {placeName}"](area.tx);
    out center;
    """
    r = requests.post(api_url, headers={'User-Agent': 'Map Visualizer Project'}, data={'data': query}, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"Overpass error {r.status_code}: {r.text[:300]}")

    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        f.write(r.text)

    return list(csv.DictReader(io.StringIO(r.text), delimiter='\t'))
