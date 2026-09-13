import sys
import os
import csv
import json
import math
import time
import xml.etree.ElementTree as ET

import requests

try:
    from pyproj import Transformer
except ImportError:
    Transformer = None

ABMARKUNG_MAP = {
    '1000': 'Marke, allgemein',
    '1100': 'Grenzstein, Granit',
    '1110': 'Grenzstein, Basalt',
    '1120': 'Grenzstein, Sandstein',
    '1130': 'Grenzstein, Beton',
    '1140': 'Kunststoffmarke',
    '1200': 'Metallmarke / Bolzen',
    '1300': 'Rohr',
    '1310': 'Vermessungsnagel / Metallkopf',
    '1400': 'Meisselzeichen',
    '1500': 'Pfahl',
    '9500': 'Nicht abgemarkt / rechnerisch',
    '9998': 'Sonstiges',
    '9999': 'unbekannt'
}

QUALITY_MAP = {
    '1000': '2 mm',
    '1200': '1 cm',
    '2000': '2 cm',
    '2100': '3 cm',
    '2200': '6 cm',
    '2300': '10 cm',
    '3000': '30 cm'
}


def format_abmarkung(code):
    label = ABMARKUNG_MAP.get(code, code)
    return f"{code} ({label})" if code not in ('', 'unbekannt') else label


def format_coordinates(east, north):
    return f"East: {east}, North: {north}"


def get_gps_coordinates(east, north):
    if Transformer is None:
        return None, None

    transformer = Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)
    lon, lat = transformer.transform(float(east), float(north))
    return lat, lon


def format_gps_coordinates(east, north):
    lat, lon = get_gps_coordinates(east, north)
    if lat is None or lon is None:
        return ""

    return f"Lat: {lat:.6f}, Lon: {lon:.6f}"


def deg2num(lat_deg, lon_deg, zoom):
    lat_rad = math.radians(lat_deg)
    n = 2.0 ** zoom
    xtile = int((lon_deg + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return xtile, ytile


def download_osm_tile(zoom, x, y, cache_dir):
    tile_path = os.path.join(cache_dir, str(zoom), str(x), f"{y}.png")

    if os.path.exists(tile_path):
        return

    url = f"https://tile.openstreetmap.org/{zoom}/{x}/{y}.png"
    headers = {
        "User-Agent": "GrenzsteinExporter/1.0 (your.email.here@gmail.com)"
    }

    response = requests.get(url, headers=headers, timeout=10)
    response.raise_for_status()

    os.makedirs(os.path.dirname(tile_path), exist_ok=True)
    with open(tile_path, mode='wb') as tile_file:
        tile_file.write(response.content)

    time.sleep(0.5)


def prepare_osm_tile_cache(map_points, cache_dir='tile_cache', min_zoom=12, max_zoom=19):
    if not map_points:
        return

    latitudes = [point['lat'] for point in map_points]
    longitudes = [point['lon'] for point in map_points]

    lat_min = min(latitudes)
    lat_max = max(latitudes)
    lon_min = min(longitudes)
    lon_max = max(longitudes)

    for zoom in range(min_zoom, max_zoom + 1):
        x1, y1 = deg2num(lat_max, lon_min, zoom)
        x2, y2 = deg2num(lat_min, lon_max, zoom)

        for x in range(min(x1, x2), max(x1, x2) + 1):
            for y in range(min(y1, y2), max(y1, y2) + 1):
                download_osm_tile(zoom, x, y, cache_dir)

    print(f"OSM-Tile-Cache vorbereitet in '{cache_dir}'.")


def format_genauigkeitsstufe(code):
    if not code:
        return ""

    quality = QUALITY_MAP.get(code, code)
    return f"{code} ({quality})"


def local_name(tag):
    return tag.rsplit('}', 1)[-1] if '}' in tag else tag


def read_first_text(elem, names):
    for child in elem.iter():
        if local_name(child.tag) in names and child.text:
            return child.text.strip()
    return ""


def extract_coords_from_text(text):
    if not text:
        return "", ""

    coords = text.strip().replace(',', ' ').split()
    if len(coords) >= 2:
        return coords[0], coords[1]

    return "", ""


def get_grenzpunkt_id(elem):
    return elem.attrib.get('{http://www.opengis.net/gml/3.2}id', elem.attrib.get('id', 'Unbekannt'))


def find_related_punktort_info(root, grenzpunkt_id):
    target_urn = f"urn:adv:oid:{grenzpunkt_id}"

    for punktort in root.iter():
        if local_name(punktort.tag) != 'AX_PunktortTA':
            continue

        related_href = ""
        for child in punktort.iter():
            if local_name(child.tag) == 'istTeilVon':
                related_href = child.attrib.get('{http://www.w3.org/1999/xlink}href', child.attrib.get('href', ''))
                break

        if not related_href:
            continue

        if related_href.endswith(grenzpunkt_id) or related_href == target_urn:
            pos_text = read_first_text(punktort, ['pos'])
            coordinates_text = read_first_text(punktort, ['coordinates'])
            east, north = extract_coords_from_text(pos_text or coordinates_text)
            genauigkeitsstufe = read_first_text(punktort, ['genauigkeitsstufe'])
            return east, north, genauigkeitsstufe

    return "", "", ""


def has_local_au(root, grenzpunkt_id):
    target_urn = f"urn:adv:oid:{grenzpunkt_id}"

    for punktort in root.iter():
        if local_name(punktort.tag) != 'AX_PunktortAU':
            continue

        related_href = ""
        for child in punktort.iter():
            if local_name(child.tag) == 'istTeilVon':
                related_href = child.attrib.get('{http://www.w3.org/1999/xlink}href', child.attrib.get('href', ''))
                break

        if related_href.endswith(grenzpunkt_id) or related_href == target_urn:
            return True

    return False


def build_besonderheiten(pointkennung, entstehung, abmarkung_code, has_au):
    notes = []

    if pointkennung:
        notes.append(f"Punktkennung: {pointkennung}")
    else:
        notes.append("Keine eigene Punktkennung vergeben")

    if entstehung:
        notes.append(f"Entstehung: {entstehung}")

    if abmarkung_code == '9500':
        notes.append("Rein rechnerischer/unabgemarkter Grenzpunkt")

    if has_au:
        notes.append("besitzt auch lokalen Punktort AU")

    return "; ".join(notes)


def parse_grenzpunkte(xml_path):
    print(f"Analysiere Datei '{xml_path}'.")
    if not os.path.exists(xml_path):
        print(f"Fehler: Datei '{xml_path}' wurde nicht gefunden.")
        sys.exit(1)

    base_name, _ = os.path.splitext(xml_path)
    csv_path = f"{base_name}.csv"

    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except ET.ParseError as e:
        print(f"Fehler beim Lesen der XML-Datei: {e}")
        sys.exit(1)

    grenzpunkte = []
    map_points = []

    punkteZaehler = 0
    for elem in root.iter():
        if local_name(elem.tag) != 'AX_Grenzpunkt':
            continue

        punkteZaehler += 1
        print(f"Grenzpunkt '{punkteZaehler}' gefunden, Attribute werden extrahiert")

        grenzpunkt_id = get_grenzpunkt_id(elem)
        punktkennung = read_first_text(elem, ['punktkennung'])
        entstehung = read_first_text(elem, ['zeitpunktDerEntstehung'])
        abmarkung_code = read_first_text(elem, ['abmarkung_Marke', 'abmarkung_marke']) or 'unbekannt'

        east, north, genauigkeitsstufe = find_related_punktort_info(root, grenzpunkt_id)
        if not east or not north:
            continue

        has_au = has_local_au(root, grenzpunkt_id)
        besonderheiten = build_besonderheiten(punktkennung, entstehung, abmarkung_code, has_au)
        lat, lon = get_gps_coordinates(east, north)

        stone_number = len(grenzpunkte) + 1

        grenzpunkte.append({
            'Stein Nr.': stone_number,
            'Grenzpunkt-ID': grenzpunkt_id,
            'Abmarkung (abmarkung_Marke)': format_abmarkung(abmarkung_code),
            'Koordinaten (ETRS89 / UTM Zone 32N)': format_coordinates(east, north),
            'GPS (WGS84 Lat/Lon)': format_gps_coordinates(east, north),
            'Genauigkeitsstufe': format_genauigkeitsstufe(genauigkeitsstufe),
            'Punktkennung / Besonderheiten': besonderheiten,
        })

        if lat is not None and lon is not None:
            map_points.append({
                'index': stone_number,
                'lat': float(lat),
                'lon': float(lon),
                'abmarkung': format_abmarkung(abmarkung_code),
            })

    if not grenzpunkte:
        print("Hinweis: Keine gültigen Grenzpunkte mit Koordinaten in der XML-Datei gefunden.")
        return None

    fieldnames = [
        'Stein Nr.',
        'Grenzpunkt-ID',
        'Abmarkung (abmarkung_Marke)',
        'Koordinaten (ETRS89 / UTM Zone 32N)',
        'GPS (WGS84 Lat/Lon)',
        'Genauigkeitsstufe',
        'Punktkennung / Besonderheiten',
    ]

    with open(csv_path, mode='w', newline='', encoding='utf-8') as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames, delimiter=';')
        writer.writeheader()
        writer.writerows(grenzpunkte)

    html_path = write_map_html(csv_path, map_points)
    if html_path:
        prepare_osm_tile_cache(map_points, cache_dir=os.path.join(os.path.dirname(os.path.abspath(html_path)), 'tile_cache'))

    print(f"Erfolgreich {len(grenzpunkte)} Grenzsteine exportiert nach: {csv_path}")
    return csv_path, html_path


def write_map_html_old(csv_path, map_points):
    if not map_points:
        print("Hinweis: Keine Koordinaten für die OSM-Karte verfügbar.")
        return None

    map_path = os.path.splitext(csv_path)[0] + '_karte.html'
    html_dir = os.path.dirname(os.path.abspath(map_path))
    tile_cache_dir = os.path.join(html_dir, 'tile_cache')
    tile_cache_url = os.path.relpath(tile_cache_dir, html_dir).replace('\\', '/') + '/{z}/{x}/{y}.png'
    points_json = json.dumps(map_points)

    html = f"""<!DOCTYPE html>
<html lang=\"de\">
<head>
    <meta charset=\"UTF-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
    <title>Grenzstein-Karte</title>
    <link rel=\"stylesheet\" href=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.css\" />
    <style>
        html, body {{ height: 100%; margin: 0; font-family: Arial, sans-serif; }}
        body {{ background: #f3f3f3; }}
        #map {{ height: 100vh; width: 100%; }}
        .leaflet-tooltip.label-tooltip {{
            background: rgba(255, 255, 255, 0.95);
            border: 1px solid #666;
            border-radius: 6px;
            padding: 4px 8px;
            font-weight: bold;
            color: #111;
            box-shadow: 0 0 6px rgba(0,0,0,0.15);
        }}
    </style>
</head>
<body>
    <div id=\"map\"></div>
    <script src=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.js\"></script>
    <script>
        const points = {points_json};
        const map = L.map('map');

        L.tileLayer('{tile_cache_url}', {{
            maxZoom: 19,
            attribution: '&copy; OpenStreetMap contributors'
        }}).addTo(map);

        const bounds = L.latLngBounds(points.map((point) => [point.lat, point.lon]));
        map.fitBounds(bounds.pad(0.25));

        points.forEach((point) => {{
            const marker = L.circleMarker([point.lat, point.lon], {{
                radius: 6,
                color: '#d62728',
                fillColor: '#d62728',
                fillOpacity: 1,
                weight: 1
            }}).addTo(map);

            marker.bindTooltip(String(point.index), {{
                permanent: true,
                direction: 'top',
                className: 'label-tooltip'
            }});

            marker.bindPopup(
                '<b>Stein Nr. ' + point.index + '</b><br>' +
                'Lat: ' + point.lat.toFixed(6) + '<br>' +
                'Lon: ' + point.lon.toFixed(6) + '<br>' +
                'Abmarkung: ' + point.abmarkung
            );
        }});
    </script>
</body>
</html>
"""

    with open(map_path, mode='w', encoding='utf-8') as map_file:
        map_file.write(html)

    print(f"OSM-Karte erzeugt: {map_path}")
    return map_path

def write_map_html(csv_path, map_points):
    if not map_points:
        print("Hinweis: Keine Koordinaten für die OSM-Karte verfügbar.")
        return None

    map_path = os.path.splitext(csv_path)[0] + '_karte.html'
    html_dir = os.path.dirname(os.path.abspath(map_path))
    tile_cache_dir = os.path.join(html_dir, 'tile_cache')
    tile_cache_url = os.path.relpath(tile_cache_dir, html_dir).replace('\\', '/') + '/{z}/{x}/{y}.png'
    points_json = json.dumps(map_points)

    html = f"""<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Grenzstein-Karte</title>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <style>
        html, body {{ height: 100%; margin: 0; font-family: Arial, sans-serif; }}
        body {{ background: #f3f3f3; }}
        #map {{ height: 100vh; width: 100%; }}
        .leaflet-tooltip.label-tooltip {{
            background: rgba(255, 255, 255, 0.95);
            border: 1px solid #666;
            border-radius: 6px;
            padding: 4px 8px;
            font-weight: bold;
            color: #111;
            box-shadow: 0 0 6px rgba(0,0,0,0.15);
        }}
    </style>
</head>
<body>
    <div id="map"></div>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script>
        const points = {points_json};
        const map = L.map('map');

        // 1. OSM Basiskarte (lokal gecacht)
        const osmLayer = L.tileLayer('{tile_cache_url}', {{
            maxZoom: 19,
            attribution: '&copy; OpenStreetMap contributors'
        }}).addTo(map);

        // 2. LVermGeo RLP WMS Overlay: Flurstücke + Gebäude in einem Aufruf
        const alkisWmsLayer = L.tileLayer.wms('https://geo5.service24.rlp.de/wms/liegenschaften_rp.fcgi', {{
            layers: 'Flurstueck,GebaeudeBauwerke',
            format: 'image/png',
            transparent: true,
            maxZoom: 19,
            attribution: '&copy; GeoBasis-DE / LVermGeo RLP'
        }}).addTo(map);

        // Layer-Steuerung oben rechts zum Ein-/Ausblenden
        L.control.layers({{
            "OpenStreetMap": osmLayer
        }}, {{
            "ALKIS (Flurstücke & Gebäude)": alkisWmsLayer
        }}).addTo(map);

        const bounds = L.latLngBounds(points.map((point) => [point.lat, point.lon]));
        map.fitBounds(bounds.pad(0.25));

        points.forEach((point) => {{
            const marker = L.circleMarker([point.lat, point.lon], {{
                radius: 6,
                color: '#d62728',
                fillColor: '#d62728',
                fillOpacity: 1,
                weight: 1
            }}).addTo(map);

            marker.bindTooltip(String(point.index), {{
                permanent: true,
                direction: 'top',
                className: 'label-tooltip'
            }});

            marker.bindPopup(
                '<b>Stein Nr. ' + point.index + '</b><br>' +
                'Lat: ' + point.lat.toFixed(6) + '<br>' +
                'Lon: ' + point.lon.toFixed(6) + '<br>' +
                'Abmarkung: ' + point.abmarkung
            );
        }});
    </script>
</body>
</html>
"""

    with open(map_path, mode='w', encoding='utf-8') as map_file:
        map_file.write(html)

    print(f"OSM-Karte erzeugt: {map_path}")
    return map_path

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Nutzung: python script.py <pfad_zur_xml_datei.xml>")
        sys.exit(1)

    xml_input = sys.argv[1]
    parse_grenzpunkte(xml_input)
