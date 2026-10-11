"""Turn Novogrudok burial PDFs or spreadsheet CSVs into maps/cemeteries/burials.json.

The page reads that file. Re-run this when a new list arrives:

    python3 maps/cemeteries/build_burials.py part1.pdf part2.pdf
    python3 maps/cemeteries/build_burials.py large.csv small.csv
    python3 maps/cemeteries/build_burials.py --append Новосады list.csv
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.mogilki_pdf import records_from_pdf
from app.services.mogilki_sheet import records_from_csv

MAP_PATH = Path(__file__).with_name("data.js")
OUT = Path(__file__).with_name("burials.json")

# Nominatim place points. The cemetery map has no pin for these two.
FALLBACK = {
    "Авдеевичи": (53.6694435, 26.1434319, "Каардынаты вёскі: асобнай меткі могілак на мапе няма."),
    "Мотча Православное": (53.60341, 25.90061, "Каардынаты вёскі: асобнай меткі могілак на мапе няма."),
}
NOTES = {
    "Любча 1Мая": "На мапе няма асобных кропак «1 Мая» і «Леніна».",
    "Любча Ленина": "На мапе няма асобных кропак «1 Мая» і «Леніна».",
    "Негневичи Новое": "Старое і новае на мапе не падпісаны, узяты дзве блізкія кропкі Негневічаў.",
    "Негневичи Старое": "Старое і новае на мапе не падпісаны, узяты дзве блізкія кропкі Негневічаў.",
    "Вселюб Православное": "Асобнай праваслаўнай кропкі няма, паказаны Уселюб.",
}

_STOP = {"православное", "католическое", "еврейское", "старое", "новое", "кладбище", "могилки", "городское"}
_QUALIFIERS = ("католическое", "православное", "еврейское")


def fold(value: str) -> str:
    text = value.casefold().replace("ё", "е").replace("і", "и").replace("ў", "у")
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _place(folded: str) -> str:
    for token in folded.split():
        if token not in _STOP and not token.isdigit():
            return token
    return ""


def _score(cemetery: str, point_name: str) -> int:
    left = fold(cemetery)
    right = fold(point_name)
    if left == right:
        return 200
    place = _place(left)
    other = _place(right)
    if not place:
        return 0
    if place == other:
        score = 80
    elif place in right.split():
        score = 50
    else:
        return 0
    right_tokens = set(right.split())
    left_tokens = set(left.split())
    for qualifier in _QUALIFIERS:
        if qualifier in right_tokens and qualifier not in left_tokens:
            score -= 40
        if qualifier in left_tokens and qualifier in right_tokens:
            score += 40
    return score


def _points() -> list[dict]:
    raw = MAP_PATH.read_text(encoding="utf-8")
    data = json.loads(raw[len("window.CEMETERY_MAP=") : -2])
    points = []
    for row in data["points"]:
        points.append(
            {
                "name": row[1],
                "lon": row[2],
                "lat": row[3],
                "povet": 0 in row[0],
            }
        )
    return points


def _candidates(name: str, points: list[dict]) -> list[dict]:
    scored = []
    for point in points:
        score = _score(name, point["name"])
        if point["povet"]:
            score += 5
        if score >= 70:
            scored.append((score, point))
    if not scored:
        return []
    best = max(item[0] for item in scored)
    return [point for score, point in scored if score == best]


def _slug(name: str, used: set[str]) -> str:
    base = fold(name).replace(" ", "-") or "cemetery"
    slug = base
    number = 2
    while slug in used:
        slug = f"{base}-{number}"
        number += 1
    used.add(slug)
    return slug


def locate(names: list[str], points: list[dict]) -> dict[str, dict]:
    grouped: dict[tuple, list[str]] = defaultdict(list)
    points_for: dict[tuple, list[dict]] = {}
    located: dict[str, dict] = {}
    for name in names:
        found = _candidates(name, points)
        if not found:
            fallback = FALLBACK.get(name)
            if fallback is None:
                located[name] = {"lat": None, "lon": None, "approx": True, "note": "Каардынаты не знойдзены."}
            else:
                lat, lon, note = fallback
                located[name] = {"lat": lat, "lon": lon, "approx": True, "note": note}
            continue
        key = tuple(sorted((point["lat"], point["lon"]) for point in found))
        grouped[key].append(name)
        points_for[key] = found
    for key, group_names in grouped.items():
        found = sorted(points_for[key], key=lambda point: (-point["lat"], point["lon"]))
        ordered = sorted(group_names)
        if len(ordered) == 1:
            name = ordered[0]
            exact = [point for point in found if fold(point["name"]) == fold(name)]
            chosen = exact[0] if exact else found[0]
            picks = [(name, chosen)]
        else:
            picks = [(name, found[index % len(found)]) for index, name in enumerate(ordered)]
        for name, point in picks:
            exact = _score(name, point["name"]) >= 200
            located[name] = {
                "lat": point["lat"],
                "lon": point["lon"],
                "approx": not exact,
                "note": "" if exact else NOTES.get(name, "Кропка на мапе супадае па назве населенага пункта."),
            }
    return located


def bundle_from_rows(rows: list[dict]) -> dict:
    rows = [row for row in rows if row["cemetery"].casefold() != "кладбище"]
    names = sorted({row["cemetery"] for row in rows})
    located = locate(names, _points())
    used: set[str] = set()
    cemeteries = []
    ids = {}
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[row["cemetery"]] += 1
    for name in names:
        place = located[name]
        slug = _slug(name, used)
        ids[name] = slug
        cemeteries.append(
            {
                "id": slug,
                "name": name,
                "lat": place["lat"],
                "lon": place["lon"],
                "approx": place["approx"],
                "note": place["note"],
                "count": counts[name],
            }
        )
    burials = []
    for row in rows:
        item = {"c": ids[row["cemetery"]]}
        for source_key, short in (
            ("surname", "s"),
            ("given", "g"),
            ("patronymic", "p"),
            ("birth", "b"),
            ("death", "d"),
        ):
            if row[source_key]:
                item[short] = row[source_key]
        if row["birth_year"]:
            item["by"] = row["birth_year"]
        if row["death_year"]:
            item["dy"] = row["death_year"]
        for source_key, short in (
            ("sector", "sec"),
            ("row", "rw"),
            ("plot", "pl"),
            ("note", "note"),
            ("url", "url"),
        ):
            if row.get(source_key):
                item[short] = row[source_key]
        if row["source"]:
            item["src"] = row["source"]
        burials.append(item)
    return {"cemeteries": cemeteries, "burials": burials}


def _date_fits(left: str, right: str) -> bool:
    if left == right or not left or not right:
        return True
    short, full = (left, right) if len(left) <= len(right) else (right, left)
    if not full.endswith(short):
        return False
    return len(full) == len(short) or full[-len(short) - 1] == "."


def _finer_date(left: str, right: str) -> str:
    if not left or (right and len(right) > len(left) and _date_fits(left, right)):
        return right
    return left


def _same_person(left: dict, right: dict) -> bool:
    if not _date_fits(left["birth"], right["birth"]) or not _date_fits(left["death"], right["death"]):
        return False
    for key in ("sector", "row", "plot"):
        if left[key] and right[key] and left[key] != right[key]:
            return False
    return True


def _absorb(kept: dict, extra: dict) -> None:
    for key in ("surname", "given", "patronymic", "sector", "row", "plot", "url", "source"):
        if not kept[key] and extra[key]:
            kept[key] = extra[key]
    kept["birth"] = _finer_date(kept["birth"], extra["birth"])
    kept["death"] = _finer_date(kept["death"], extra["death"])
    if kept["birth_year"] is None:
        kept["birth_year"] = extra["birth_year"]
    if kept["death_year"] is None:
        kept["death_year"] = extra["death_year"]
    note = extra["note"]
    if note and note.casefold() not in kept["note"].casefold():
        kept["note"] = (kept["note"] + ". " + note).strip(". ") if kept["note"] else note


def dedupe_people(rows: list[dict]) -> list[dict]:
    """Collapse a second reading of the same person. Keep the fuller dates and notes."""
    grouped: dict[tuple, list[int]] = defaultdict(list)
    kept: list[dict] = []
    for row in rows:
        key = (
            fold(row["surname"]),
            fold(row["given"]),
            fold(row["patronymic"]),
            row["birth_year"],
            row["death_year"],
        )
        match = next((index for index in grouped[key] if _same_person(kept[index], row)), None)
        if match is None:
            grouped[key].append(len(kept))
            kept.append(row)
            continue
        _absorb(kept[match], row)
    return kept


def _write(payload: dict) -> None:
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def append_cemetery(name: str, path: str) -> None:
    """Add one cemetery's rows to the existing bundle. A repeat replaces that cemetery only."""
    rows = records_from_csv(Path(path), "", name)
    for row in rows:
        row["cemetery"] = name
    rows = dedupe_people(rows)
    fresh = bundle_from_rows(rows)
    existing = json.loads(OUT.read_text(encoding="utf-8")) if OUT.is_file() else {"cemeteries": [], "burials": []}
    old_ids = {item["id"] for item in existing["cemeteries"] if item["name"] == name}
    existing["cemeteries"] = [item for item in existing["cemeteries"] if item["name"] != name]
    existing["burials"] = [item for item in existing["burials"] if item.get("c") not in old_ids]
    used = {item["id"] for item in existing["cemeteries"]}
    for cemetery in fresh["cemeteries"]:
        if cemetery["id"] in used:
            raise SystemExit(f"cemetery id already used: {cemetery['id']}")
        existing["cemeteries"].append(cemetery)
    existing["burials"].extend(fresh["burials"])
    _write(existing)
    print(f"appended {name}: burials {len(fresh['burials'])} cemeteries {len(existing['cemeteries'])} -> {OUT}")
    for cemetery in fresh["cemeteries"]:
        print(f"  {cemetery['count']:5} {cemetery['name']} {cemetery['lat']} {cemetery['lon']} {cemetery['note']}")


def _rows_from(path: str) -> list[dict]:
    file = Path(path)
    if file.suffix.casefold() == ".csv":
        rows = records_from_csv(file, "")
        large = {"Щорсы", "Негневичи Новое", "Валевка", "Любча Ленина"}
        label = "крупнейшие" if any(row["cemetery"] in large for row in rows) else "сельсоветы"
        for row in rows:
            row["source"] = label
        return rows
    label = "сельсоветы" if "ч2" in file.name or "сельсовет" in file.name.casefold() else "крупнейшие"
    return records_from_pdf(file, label)


def main(paths: list[str]) -> None:
    rows = []
    for path in paths:
        rows.extend(_rows_from(path))
    payload = bundle_from_rows(rows)
    _write(payload)
    print(f"cemeteries {len(payload['cemeteries'])} burials {len(payload['burials'])} -> {OUT}")
    for cemetery in payload["cemeteries"]:
        print(f"  {cemetery['count']:5} {cemetery['name']} {cemetery['lat']} {cemetery['lon']} {cemetery['note']}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--append":
        if len(sys.argv) != 4:
            raise SystemExit("usage: build_burials.py --append CEMETERY file.csv")
        append_cemetery(sys.argv[2], sys.argv[3])
    else:
        main(sys.argv[1:])
