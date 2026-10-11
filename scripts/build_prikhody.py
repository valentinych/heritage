#!/usr/bin/env python3
"""Build the prikhody parish catalog for «Города и веси».

Reads the published parish list (prikhody-main.json) and the Firebase
``prikhods`` export (documents and the settlements each parish served).
Writes maps/cemeteries/prikhody_parishes.json. The app attaches parishes
at startup; this script does not pick settlements.

A digitized NIAB file is marked from digitedFormattedData.json. An optional
saved parish page can add the rejected-in-archive flag the static page shows.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SOURCE = "https://shappoff.github.io/prikhody/"
PAGE = SOURCE + "p/"


def plain(value) -> str:
    text = value if isinstance(value, str) else ""
    text = re.sub(r"<[^>]*>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def rite_of(church: str) -> str:
    text = church.casefold().replace("ё", "е")
    if "старообряд" in text:
        return "старообрядческий"
    if "церковь" in text:
        return "православный"
    if "костел" in text:
        return "католический"
    return "католический" if church else ""


def coord(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    if number == 0.0:
        return None
    return number


def split_archive(description: str) -> tuple[str, str, str, str]:
    text = plain(description)
    archive = text
    fod = ""
    if "ФОД:" in text:
        archive, fod = text.split("ФОД:", 1)
        archive = archive.strip(" ,")
        fod = fod.strip()
    bits = [part for part in fod.split("-") if part]
    fond = bits[0] if bits else ""
    opis = bits[1] if len(bits) > 1 else ""
    delo = bits[2] if len(bits) > 2 else ""
    return archive, fond, opis, delo


def https(value) -> str:
    if isinstance(value, str) and value.startswith("https://"):
        return value
    return ""


def load_digitized(path: str) -> dict:
    if not path:
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def is_digitized(table: dict, archive: str, fond: str, opis: str, delo: str) -> bool:
    if archive.split()[:1] != ["НИАБ"] or not fond or not opis or not delo:
        return False
    fond_row = table.get(fond)
    if not isinstance(fond_row, dict):
        return False
    opis_row = fond_row.get(opis)
    if not isinstance(opis_row, dict):
        return False
    return bool(opis_row.get(delo))


def documents_of(rows: list, digitized: dict) -> list[dict]:
    found = []
    seen = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        year = plain(row.get("year"))
        kind = plain(row.get("note"))
        archive, fond, opis, delo = split_archive(row.get("description") or "")
        fod = "-".join(part for part in (fond, opis, delo) if part)
        url = https(row.get("link"))
        key = (year, kind, archive, fod, url)
        if key in seen:
            continue
        if not any(key):
            continue
        seen.add(key)
        item = {
            "year": year,
            "type": kind,
            "archive": archive,
            "archive_name": plain(row.get("arTitle")),
            "fod": fod,
            "url": url,
        }
        if is_digitized(digitized, archive, fond, opis, delo):
            item["digitized"] = True
        found.append(item)
    return found


def served_of(rows: list) -> list[str]:
    found = []
    seen = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        title = plain(row.get("title"))
        if not title:
            continue
        key = title.casefold().replace("ё", "е")
        if key in seen:
            continue
        seen.add(key)
        found.append(title)
    return found


def overlay_rejected(parishes: list[dict], html_path: str) -> None:
    """Copy the rejected flag from one saved parish page, matched by row."""
    if not html_path:
        return
    text = Path(html_path).read_text(encoding="utf-8")
    chunks = re.findall(r"self\.__next_f\.push\((.*?)\)\s*</script>", text, re.S)
    blob = []
    for chunk in chunks:
        try:
            val = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(val, list) and len(val) > 1 and isinstance(val[1], str):
            blob.append(val[1])
    raw = "".join(blob)
    marker = '"archives":'
    start = raw.find(marker)
    if start < 0:
        return
    start = raw.find("[", start)
    depth = 0
    end = start
    for index, char in enumerate(raw[start:], start):
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    try:
        rows = json.loads(raw[start:end].replace('"$undefined"', "null"))
    except json.JSONDecodeError:
        return
    prikhod_at = raw.find('"prikhod":')
    parish_id = ""
    if prikhod_at >= 0:
        match = re.search(r'"prikhod":\["([^"]+)"', raw[prikhod_at : prikhod_at + 200])
        if match:
            parish_id = match.group(1)
    parish = next((item for item in parishes if item["id"] == parish_id), None)
    if parish is None:
        return
    rejected = set()
    for row in rows:
        if not isinstance(row, dict) or not row.get("isRejected"):
            continue
        fod = "-".join(
            part
            for part in (plain(row.get("fond")), plain(row.get("opis")), plain(row.get("delo")))
            if part
        )
        rejected.add((plain(row.get("year")), plain(row.get("type")), fod, https(row.get("link"))))
    for document in parish["documents"]:
        key = (document["year"], document["type"], document["fod"], document["url"])
        if key in rejected:
            document["rejected"] = True


def build(main_rows: list, firebase: dict, digitized: dict) -> list[dict]:
    parishes = []
    for row in main_rows:
        if not isinstance(row, list) or not row or not isinstance(row[0], str) or not row[0]:
            continue
        parish_id = row[0]
        church = plain(row[1] if len(row) > 1 else "")
        place = plain(row[2] if len(row) > 2 else "")
        place_type = plain(row[3] if len(row) > 3 else "")
        lat = coord(row[4] if len(row) > 4 else None)
        lon = coord(row[5] if len(row) > 5 else None)
        atd_raw = row[7] if len(row) > 7 and isinstance(row[7], str) else ""
        atd = [plain(part) for part in atd_raw.split("|") if plain(part)]
        remote = firebase.get(parish_id) if isinstance(firebase, dict) else None
        if not isinstance(remote, dict):
            remote = {}
        if lat is None or lon is None:
            geo = remote.get("_geoloc") if isinstance(remote.get("_geoloc"), dict) else {}
            lat = coord(geo.get("lat"))
            lon = coord(geo.get("lng"))
        if church and place:
            name = f"{place}, {church}"
        else:
            name = place or church or parish_id
        parishes.append(
            {
                "id": parish_id,
                "name": name,
                "place": place,
                "place_type": place_type,
                "church": church,
                "rite": rite_of(church),
                "lat": lat,
                "lon": lon,
                "atd": atd,
                "url": PAGE + parish_id,
                "served": served_of(remote.get("nps") or []),
                "documents": documents_of(remote.get("archives") or [], digitized),
            }
        )
    parishes.sort(key=lambda item: item["id"])
    return parishes


def main() -> None:
    parser = argparse.ArgumentParser(description="Write the prikhody parish catalog.")
    parser.add_argument("--main", required=True, help="prikhody-main.json")
    parser.add_argument("--firebase", required=True, help="prikhods.json from Firebase")
    parser.add_argument("--digitized", default="", help="digitedFormattedData.json")
    parser.add_argument("--html", default="", help="one saved parish page, for the rejected flag")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    main_rows = json.loads(Path(args.main).read_text(encoding="utf-8"))
    firebase = json.loads(Path(args.firebase).read_text(encoding="utf-8"))
    digitized = load_digitized(args.digitized)
    parishes = build(main_rows, firebase, digitized)
    overlay_rejected(parishes, args.html)
    documents = sum(len(item["documents"]) for item in parishes)
    payload = {
        "source": SOURCE,
        "fetched": "2026-10-10",
        "parish_count": len(parishes),
        "document_count": documents,
        "with_coordinates": sum(1 for item in parishes if item["lat"] is not None and item["lon"] is not None),
        "parishes": parishes,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"parishes {len(parishes)} documents {documents} coords {payload['with_coordinates']} -> {out}")


if __name__ == "__main__":
    main()
