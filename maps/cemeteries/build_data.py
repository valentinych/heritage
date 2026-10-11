"""Turn the Google My Maps KML export into maps/cemeteries/data.js.

Source map: https://www.google.com/maps/d/viewer?mid=1RZdftGg3brwVAu4vTBV10VlrAjxdNXs
"""

from __future__ import annotations

import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

KML_URL = (
    "https://www.google.com/maps/d/kml?mid=1RZdftGg3brwVAu4vTBV10VlrAjxdNXs&forcekml=1"
)
NS = {"k": "http://www.opengis.net/kml/2.2"}
OUT = Path(__file__).with_name("data.js")
UYE_PATH = Path(__file__).with_name("navahrudak-uyezd.json")
BURIALS_PATH = Path(__file__).with_name("burials.json")
INDEX_LAYER = {
    "id": "indexed",
    "name": "Праіндэксаваныя могілкі",
    "color": "#0F766E",
    "marker": "pin",
    "on": True,
}
POVE_LAYER = {
    "id": "navahrudak",
    "name": "Навагрудскі павет",
    "color": "#9A3412",
    "on": True,
}

# Dark fills so cluster counts stay readable in white.
PALETTE = [
    "#37474F",
    "#01579B",
    "#1B5E20",
    "#BF360C",
    "#B71C1C",
    "#4A148C",
    "#006064",
    "#880E4F",
    "#4E342E",
    "#1A237E",
]

COORD_INLINE = re.compile(
    r"(?i)\b(longtitude|longitude|latitude)\s*:\s*(?:[-+]?\d+(?:\.\d+)?)?"
)
JUNK_LINE = re.compile(r"(?i)^(описание|longtitude|longitude|latitude)\s*:?$")


def clean_note(raw: str) -> str:
    if not raw:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    text = re.sub(
        r"(?i)<a\s+[^>]*href=[\"']([^\"']+)[\"'][^>]*>.*?</a>",
        r"\1",
        text,
        flags=re.S,
    )
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    kept: list[str] = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        line = re.sub(r"(?i)^описание\s*:\s*", "", line)
        line = COORD_INLINE.sub("", line)
        line = re.sub(r"\s+", " ", line).strip()
        if not line or JUNK_LINE.fullmatch(line) or (kept and kept[-1] == line):
            continue
        kept.append(line)
    return "\n".join(kept)


def point_in_ring(lon: float, lat: float, ring: list[list[float]]) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > lat) != (y2 > lat):
            xinter = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            if lon < xinter:
                inside = not inside
    return inside


def load_kml(path: Path | None) -> bytes:
    if path is not None:
        return path.read_bytes()
    with urllib.request.urlopen(KML_URL, timeout=60) as response:
        return response.read()


def _fold(value: str) -> str:
    text = value.casefold().replace("ё", "е").replace("і", "и").replace("ў", "у")
    return re.sub(r"\s+", " ", text).strip()


def attach_indexed(payload: dict) -> None:
    """Mark cemeteries that have a burial list, and add any the map does not have."""
    if not BURIALS_PATH.is_file():
        return
    burials = json.loads(BURIALS_PATH.read_text(encoding="utf-8"))
    layers = payload["layers"]
    points = payload["points"]
    ring = payload.get("uyezd", {}).get("ring") or []
    index = next((i for i, layer in enumerate(layers) if layer.get("id") == INDEX_LAYER["id"]), None)
    if index is None:
        layers.append(dict(INDEX_LAYER))
        index = len(layers) - 1
    else:
        layers[index].update(INDEX_LAYER)
    for cemetery in burials.get("cemeteries", []):
        lat = cemetery.get("lat")
        lon = cemetery.get("lon")
        if lat is None or lon is None:
            continue
        count = cemetery.get("count") or 0
        label = f"У спісе {count} пахаванняў."
        hit = None
        best = 1e-4
        for row in points:
            distance = max(abs(row[2] - lon), abs(row[3] - lat))
            if distance < best:
                best = distance
                hit = row
        if hit is None:
            membership = [index]
            if ring and point_in_ring(lon, lat, ring):
                membership.insert(0, 0)
            note = cemetery.get("note") or ""
            text = (note + "\n" + label).strip() if note else label
            points.append([membership, cemetery["name"], round(lon, 6), round(lat, 6), text])
            continue
        if index not in hit[0]:
            hit[0].append(index)
        note = hit[4] if len(hit) > 4 else ""
        extra = label if _fold(hit[1]) == _fold(cemetery["name"]) else f"{cemetery['name']}: {label}"
        if extra in note:
            continue
        hit_note = (note + "\n" + extra).strip() if note else extra
        if len(hit) > 4:
            hit[4] = hit_note
        else:
            hit.append(hit_note)


def write_payload(payload: dict) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoded = encoded.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029").replace("</", "<\\/")
    OUT.write_text("window.CEMETERY_MAP=" + encoded + ";\n", encoding="utf-8")


def patch_indexed() -> None:
    raw = OUT.read_text(encoding="utf-8")
    payload = json.loads(raw[len("window.CEMETERY_MAP=") : -2])
    attach_indexed(payload)
    write_payload(payload)
    layers = payload["layers"]
    points = payload["points"]
    index = next(i for i, layer in enumerate(layers) if layer["id"] == "indexed")
    count = sum(1 for row in points if index in row[0])
    print(f"indexed layer: {count} points, total {len(points)}")


def main() -> None:
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    root = ET.fromstring(load_kml(source))
    doc = root.find("k:Document", NS)
    if doc is None:
        raise SystemExit("KML has no Document")

    uyezd = json.loads(UYE_PATH.read_text(encoding="utf-8"))
    ring = uyezd["ring"]
    layers = []
    points = []
    skipped = 0
    for index, folder in enumerate(doc.findall("k:Folder", NS)):
        name = folder.findtext("k:name", default=f"Слой {index + 1}", namespaces=NS).strip()
        layers.append({
            "id": f"l{index}",
            "name": name,
            "color": PALETTE[index % len(PALETTE)],
            "on": False,
        })
        for placemark in folder.findall("k:Placemark", NS):
            coord = placemark.findtext(".//k:coordinates", default="", namespaces=NS)
            parts = coord.replace("\n", " ").split(",")
            if len(parts) < 2:
                skipped += 1
                continue
            try:
                lon = round(float(parts[0].strip()), 6)
                lat = round(float(parts[1].strip()), 6)
            except ValueError:
                skipped += 1
                continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                skipped += 1
                continue
            title = (placemark.findtext("k:name", default="", namespaces=NS) or "").strip()
            note = clean_note(placemark.findtext("k:description", default="", namespaces=NS) or "")
            if not note:
                extra = placemark.findtext(
                    "k:ExtendedData/k:Data[@name='описание']/k:value",
                    default="",
                    namespaces=NS,
                )
                note = clean_note(extra or "")
            membership = [index + 1]
            if point_in_ring(lon, lat, ring):
                membership.insert(0, 0)
            row = [membership, title, lon, lat]
            if note:
                row.append(note)
            points.append(row)

    layers.insert(0, POVE_LAYER)
    payload = {
        "title": (doc.findtext("k:name", default="Мапа могілак Беларусі", namespaces=NS) or "").strip(),
        "layers": layers,
        "uyezd": {"layer": POVE_LAYER["id"], "ring": ring},
        "points": points,
    }
    attach_indexed(payload)
    write_payload(payload)
    noted = sum(1 for row in points if len(row) > 4)
    print(f"wrote {OUT} layers={len(layers)} points={len(points)} notes={noted} skipped={skipped}")
    for index, layer in enumerate(layers):
        count = sum(1 for row in points if index in row[0])
        print(f"  {count:5} {layer['name']}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--indexed":
        patch_indexed()
    else:
        main()
