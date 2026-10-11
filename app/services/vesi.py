"""Settlements for «Города и веси».

Map points stay in maps/cemeteries/data.js. Burial lists stay in burials.json.
A marker is a settlement: nearby cemeteries that clearly share a village name
share one marker. A burial's place (НП) wins when the record has one,
for example «Подлесейки, Новогрудский район» → Подлесейки.
A cemetery with no coordinates, or whose name is not a settlement, stays
unbound until a row in artifact_settlements gives it a settlement.
Those rows are also read from maps/cemeteries/artifact_settlements.json
and inserted on startup when the database does not already have them.
Settlement titles keep the village and leave cemetery words on the artifact.

Indexed means a burial list matched to a point on the indexed layer
(lat/lon within about 0.0002). Only those cemeteries are «Свободный доступ».
A parish from geneteka_parishes.json is an artifact too. It is attached only
when that file marks it verified and names one settlement id that already
exists. Any other Geneteka parish stays unbound. A parish with a Geneteka
index is «Свободный доступ» and links there. Person rows are not stored.
A parish from prikhody_parishes.json keeps its documents on the artifact.
It joins the nearest settlement when that place agrees: the name matches
within 15 km, or the point is within 4 km. A point inside a city boundary
joins that city. Otherwise a settlement is added at the catalog coordinates,
titled with the catalog place or territory. A parish with documents or a
source page is «Свободный доступ».
A settlement marker is red when no artifact has information, yellow when
information covers strictly less than 25% of its artifacts, and green at
25% and above. Any status other than «Нет информации» counts.
Markers use settlement coordinates only.
City boundaries in maps/cemeteries/cities.json put every artifact inside
a city on one marker. The title is the Russian name (Гродно, not the
Belarusian Гродна on the boundary). An artifact outside those boundaries
stays on the settlement the village grouping already gave it.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict

from app.services.mogilki import ROOT as CEMETERY_ROOT
from app.services.mogilki import load_bundle, load_map

# A burial list belongs to a map pin when lat and lon are within about 0.0002.
_MATCH = 0.0002
# Cemeteries of one village sit a couple of kilometres apart (Любча is about 2.4).
_MAX_KM = 4.0

# A bare «Кладбище» is not a shared village name.
_UNGROUPED = {"", "?", "cemetery", "cmentarz", "кладбище", "могилки"}
_PLACE_KEYS = ("np", "place", "loc", "нп")
# Words that name the burial ground. They never belong on the settlement.
_CEMETERY_NOUNS = {
    "кладбище",
    "кладбища",
    "кладбищу",
    "кладбищем",
    "кладбищ",
    "кладбишча",
    "кладбищенская",
    "кладбищенское",
    "кладбищенские",
    "кладбищенский",
    "кладбищинская",
    "кладбищинское",
    "кладбищинский",
    "могильник",
    "могильника",
    "захоронение",
    "захоронения",
    "могилки",
    "могила",
    "могилы",
    "могилу",
    "могил",
    "cmentarz",
    "cmentarza",
    "cmentarzu",
    "cmenatrz",
    "cemetery",
    "friedhof",
    "friedhofs",
    "cemeteries",
    "kirkut",
    "kirkuta",
    "kladbishche",
    "мизар",
    "мизара",
    "часовня",
    "часовни",
}
# Cemetery kind, not a village, even when it is the whole name.
_CEMETERY_KIND = {
    "полевое",
    "полевае",
    "еврейское",
    "еврейский",
    "еврейская",
    "еврейских",
    "евреям",
    "евреев",
    "еврейского",
    "еврейскому",
    "еврейским",
    "яурэйскае",
    "яурэйския",
    "яурэйский",
    "габрэйския",
    "габрэйская",
    "габрэйскае",
    "габрэйски",
    "католическое",
    "католицкае",
    "католический",
    "католическая",
    "католицкий",
    "каталицкия",
    "каталицкая",
    "каталицкае",
    "каталицки",
    "katolicki",
    "katolickie",
    "katolicka",
    "православное",
    "праваслаунае",
    "праваслауныя",
    "праваслауны",
    "православный",
    "православная",
    "военное",
    "ваеннае",
    "воинское",
    "гарнизонное",
    "гарнизоннае",
    "братская",
    "братское",
    "братския",
    "братэрския",
    "вайсковыя",
    "jewish",
    "judischer",
    "judische",
    "judisches",
    "juedischer",
    "catholic",
    "orthodox",
    "military",
    "old",
    "parafialny",
    "rodowy",
    "zolnierzy",
    "wojska",
    "polskiego",
    "world",
    "war",
    "ws",
    "через",
    "забор",
    "луговое",
    "войсковое",
    "солдатское",
    "солдатский",
    "курганны",
    "курганный",
    "курганная",
    "столетие",
    "столетия",
}
# May be a cemetery adjective or the start of a real name («Старое Село»).
_CEMETERY_ADJECTIVE = {
    "старое",
    "старая",
    "старыя",
    "старые",
    "старый",
    "старинное",
    "новое",
    "новая",
    "новый",
    "новые",
    "новае",
    "татарское",
    "татарския",
    "городское",
    "лесное",
    "центральное",
    "малое",
    "христианское",
    "христианские",
    "христианский",
    "христианская",
    "польское",
    "немецкое",
    "старообрядческое",
    "старообрядческие",
    "мемориальное",
    "гражданское",
    "сельское",
    "деревенское",
    "фамильное",
    "шляхетское",
    "панския",
    "1мая",
    "ленина",
}
_GLUE = {"w", "we", "na", "in", "of", "the", "у", "в", "во", "г", "д", "дер", "z", "на", "вески", "деревни", "деревня", "ур"}
_GRAVE = {
    "жертв",
    "фашизма",
    "нацизма",
    "советских",
    "воинов",
    "воина",
    "партизан",
    "партизана",
    "активистов",
    "мирных",
    "жителей",
    "жителя",
    "нацистской",
    "оккупации",
    "солдат",
    "солдата",
    "белорусских",
    "польских",
    "украинских",
    "русских",
    "войны",
    "война",
    "мировой",
    "первой",
    "второй",
    "годов",
    "года",
    "год",
    "заброшенного",
    "заброшенное",
    "бывшее",
    "бывшего",
    "бывший",
    "территория",
    "территории",
    "номер",
    "французских",
    "неизвестному",
    "солдату",
    "больше",
    "ст",
}
# These adjectives are a cemetery word when nothing else names a village.
# «Новое Село» still keeps «новое», because «село» is the place.
_NEVER_ALONE = {
    "старое",
    "старая",
    "старыя",
    "старые",
    "старый",
    "старинное",
    "новое",
    "новая",
    "новый",
    "новые",
    "новае",
    "городское",
    "лесное",
    "центральное",
    "малое",
    "христианское",
    "христианские",
    "христианский",
    "христианская",
    "польское",
    "немецкое",
    "1мая",
    "ленина",
}
_ORDINAL = {"е", "й", "я", "ое", "ая", "ый", "ой"}
_ROMAN = {
    "i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x",
    "xi", "xii", "xiii", "xiv", "xv", "xvi", "xvii", "xviii", "xix", "xx",
}
BINDINGS_PATH = CEMETERY_ROOT / "artifact_settlements.json"
CITIES_PATH = CEMETERY_ROOT / "cities.json"
PARISHES_PATH = CEMETERY_ROOT / "geneteka_parishes.json"
PRIKHODY_PATH = CEMETERY_ROOT / "prikhody_parishes.json"
PARISH_KIND = "parish"
# The nearest settlement is the same place inside this radius even if the
# title differs (a church in a town). A matching name may sit a little farther.
_ATTACH_KM = 4.0
_NAME_KM = 15.0
_BARE_TITLES = {"кладбище", "парафия", "парафія", "приход", "прыход"}
_ACT_LETTER = {"р": "рождения", "б": "браки", "у": "умершие"}
_ACT_LABEL = {
    "births": "рождения",
    "marriages": "браки",
    "deaths": "смерти",
}

FREE_ACCESS = {"emoji": "✅", "label": "Свободный доступ"}
NO_INFO = {"emoji": "❔", "label": "Нет информации"}
_MARKER_CLASS = {
    "red": "vesi-dot-red",
    "yellow": "vesi-dot-yellow",
    "green": "vesi-dot-green",
}
_MARKER_LABEL = {
    "red": "нет сведений",
    "yellow": "мало сведений",
    "green": "есть сведения",
}

_index: dict | None = None
_parishes: list[dict] | None = None
_prikhody: list[dict] | None = None
_cities: list[dict] | None = None


def fold(value: str) -> str:
    text = value.casefold().replace("ё", "е").replace("і", "и").replace("ў", "у")
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def settlement_from_place(value: str) -> str:
    """«Подлесейки, Новогрудский район» → Подлесейки."""
    head = value.split(",")[0].strip()
    return re.sub(r"\s+", " ", head)


def _tokens(value: str) -> list[str]:
    text = value.casefold().replace("ё", "е").replace("і", "и").replace("ў", "у")
    text = re.sub(r"\([^)]*(?:не существует|не сохранилось)[^)]*\)", " ", text)
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    return [part for part in text.split() if part]


def _word(token: str) -> str:
    text = token.casefold().replace("ё", "е").replace("і", "и").replace("ў", "у")
    for src, dst in (
        ("ł", "l"),
        ("ś", "s"),
        ("ć", "c"),
        ("ń", "n"),
        ("ź", "z"),
        ("ż", "z"),
        ("ó", "o"),
        ("ą", "a"),
        ("ę", "e"),
        ("ü", "u"),
        ("ö", "o"),
        ("ä", "a"),
        ("ß", "ss"),
    ):
        text = text.replace(src, dst)
    return re.sub(r"[^0-9a-zа-я]+", "", text)


def _place_flags(words: list[str]) -> list[bool]:
    return [
        word not in _CEMETERY_NOUNS
        and word not in _CEMETERY_KIND
        and word not in _CEMETERY_ADJECTIVE
        and word not in _GLUE
        and word not in _GRAVE
        and not word.isdigit()
        and len(word) > 1
        for word in words
    ]


def _prefixed_place(index: int, words: list[str], place_flags: list[bool]) -> int | None:
    cursor = index + 1
    while cursor < len(words) and words[cursor] in _CEMETERY_ADJECTIVE:
        cursor += 1
    if cursor < len(words) and place_flags[cursor]:
        return cursor
    return None


def _keep_adjective(index: int, words: list[str], place_flags: list[bool], has_noun: bool) -> bool:
    """«Старое Село» keeps the adjective. «Кладбище (старое) Заславль» does not."""
    place_at = _prefixed_place(index, words, place_flags)
    if place_at is None:
        return not has_noun and not any(place_flags) and words[index] not in _NEVER_ALONE and len(words) == 1
    if not has_noun:
        return True
    cemetery_before = any(
        words[cursor] in _CEMETERY_NOUNS or words[cursor] in _CEMETERY_KIND for cursor in range(place_at)
    )
    return not cemetery_before


def place_title(name: str) -> str:
    """Village spelling. Empty when the name is only cemetery words.

    «Новосады полевое» → Новосады. «Старое Село» stays, because «старое»
    begins the village. «еврейское кладбище через забор» has no village.
    """
    text = re.sub(r"\([^)]*(?:не существует|не сохран|не факт)[^)]*\)", " ", name, flags=re.IGNORECASE)
    raw = re.findall(r"[0-9A-Za-zА-Яа-яЁёІіЎўŁłÓóŚśĆćŃńŹźŻżĄąĘęÜüÖöÄäẞß]+", text)
    if not raw:
        return ""
    words = [_word(token) for token in raw]
    has_noun = any(word in _CEMETERY_NOUNS for word in words)
    place_flags = _place_flags(words)
    has_place = any(place_flags)
    kept: list[int] = []
    for index, word in enumerate(words):
        if not word or word in _CEMETERY_NOUNS or word in _CEMETERY_KIND or word in _GRAVE:
            continue
        if word.isdigit() or re.fullmatch(r"\d+[a-zа-я]?", word):
            continue
        if word in _ROMAN:
            continue
        if word in _ORDINAL and index > 0 and words[index - 1].isdigit():
            continue
        if len(word) <= 1 and len(words) > 1:
            continue
        if word in _GLUE and (has_noun or has_place):
            continue
        if word in _CEMETERY_ADJECTIVE:
            if _keep_adjective(index, words, place_flags, has_noun):
                kept.append(index)
            continue
        kept.append(index)
    if not kept:
        return ""
    if kept == list(range(len(raw))):
        return re.sub(r"\s+", " ", text).strip(" ,.;")
    return " ".join(raw[index] for index in kept)


def village_key(name: str) -> str:
    return fold(place_title(name))


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dy = (lat1 - lat2) * 111.0
    dx = (lon1 - lon2) * 111.0 * math.cos(math.radians((lat1 + lat2) / 2.0))
    return math.hypot(dx, dy)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    step = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * radius * math.asin(min(1.0, math.sqrt(step)))


def _can_place(artifact: dict) -> bool:
    """Grouping places a cemetery that has coordinates and a village name.

    A name in _UNGROUPED is stored as a key starting with '#'. That cemetery,
    and one without coordinates, stays unbound until it is given a settlement.
    """
    if artifact["lat"] is None or artifact["lon"] is None:
        return False
    return not str(artifact["key"]).startswith("#")


def nearest_settlements(lat: float, lon: float, settlements: list[dict], limit: int = 4) -> list[dict]:
    scored = []
    for item in settlements:
        if item.get("lat") is None or item.get("lon") is None:
            continue
        distance = haversine_km(lat, lon, float(item["lat"]), float(item["lon"]))
        scored.append((distance, item["id"], item["name"]))
    scored.sort()
    return [
        {"id": item_id, "name": name, "km": round(distance, 1)}
        for distance, item_id, name in scored[:limit]
    ]


def _slug(value: str) -> str:
    parts = _tokens(value)
    return "-".join(parts) or "point"


def _settlement_id(label: str, lat: float, lon: float, used: set[str]) -> str:
    settlement_id = f"{_slug(label)}-{int(round(lat * 1000))}-{int(round(lon * 1000))}"
    base_id = settlement_id
    number = 2
    while settlement_id in used:
        settlement_id = f"{base_id}-{number}"
        number += 1
    used.add(settlement_id)
    return settlement_id


def _point_in_ring(lon: float, lat: float, ring: list) -> bool:
    """Ray cast. Ring is [lon, lat] pairs, the same test as the uyezd outline."""
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > lat) != (y2 > lat):
            xinter = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            if lon < xinter:
                inside = not inside
    return inside


def load_cities() -> list[dict]:
    """City territories. A second call returns the same rows."""
    global _cities
    if _cities is None:
        _cities = []
        if CITIES_PATH.is_file():
            payload = json.loads(CITIES_PATH.read_text(encoding="utf-8"))
            for row in payload.get("cities") or []:
                outers = row.get("outers") or []
                name = row.get("name")
                if not outers or not isinstance(name, str) or not name.strip():
                    continue
                points = [pt for ring in outers for pt in ring]
                lons = [pt[0] for pt in points]
                lats = [pt[1] for pt in points]
                _cities.append(
                    {
                        "name": name.strip(),
                        "lat": float(row["lat"]),
                        "lon": float(row["lon"]),
                        "outers": outers,
                        "holes": row.get("holes") or [],
                        "bbox": (min(lons), min(lats), max(lons), max(lats)),
                    }
                )
    return _cities


def _city_at(lat: float, lon: float) -> dict | None:
    """The city whose territory contains this point, or none."""
    for city in load_cities():
        min_lon, min_lat, max_lon, max_lat = city["bbox"]
        if lon < min_lon or lon > max_lon or lat < min_lat or lat > max_lat:
            continue
        if any(_point_in_ring(lon, lat, hole) for hole in city["holes"]):
            continue
        if any(_point_in_ring(lon, lat, ring) for ring in city["outers"]):
            return city
    return None


def _place_label(row: dict) -> str:
    for key in _PLACE_KEYS:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return settlement_from_place(value)
    return ""


def _surnames_by_cemetery(burials: list[dict]) -> dict[str, list[str]]:
    """`s` is the Russian surname when the sheet had one. Empty and nid are not surnames."""
    seen: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for row in burials:
        raw = (row.get("s") or "").strip()
        if not raw or raw.casefold() == "nid":
            continue
        seen[row["c"]][fold(raw)][raw] += 1
    result: dict[str, list[str]] = {}
    for cemetery_id, variants in seen.items():
        names = []
        for counter in variants.values():
            best = sorted(counter.items(), key=lambda item: (-item[1], len(item[0]), item[0]))[0][0]
            names.append(best)
        names.sort(key=fold)
        result[cemetery_id] = names
    return result


def _places_by_cemetery(burials: list[dict]) -> dict[str, str]:
    found: dict[str, Counter] = defaultdict(Counter)
    for row in burials:
        label = _place_label(row)
        if label:
            found[row["c"]][label] += 1
    return {cemetery_id: counter.most_common(1)[0][0] for cemetery_id, counter in found.items()}


def _match_cemeteries(points: list, cemeteries: list[dict]) -> dict[int, dict]:
    pairs = []
    for cemetery in cemeteries:
        lat = cemetery.get("lat")
        lon = cemetery.get("lon")
        if lat is None or lon is None:
            continue
        best_i = None
        best_d = _MATCH
        for index, point in enumerate(points):
            distance = max(abs(point[2] - lon), abs(point[3] - lat))
            if distance <= best_d:
                best_d = distance
                best_i = index
        if best_i is not None:
            pairs.append((best_d, best_i, cemetery))
    pairs.sort(key=lambda item: item[0])
    used_points: set[int] = set()
    used_ids: set[str] = set()
    matched: dict[int, dict] = {}
    for _distance, index, cemetery in pairs:
        if index in used_points or cemetery["id"] in used_ids:
            continue
        used_points.add(index)
        used_ids.add(cemetery["id"])
        matched[index] = cemetery
    return matched


def _on_indexed_layer(point, layer_index: int | None) -> bool:
    if layer_index is None:
        return False
    membership = point[0] if isinstance(point[0], list) else [point[0]]
    return layer_index in membership


def _map_id(name: str, lat: float, lon: float, used: set[str]) -> str:
    base = f"m-{_slug(name)}-{int(round(lat * 100000))}-{int(round(lon * 100000))}"
    artifact_id = base
    number = 2
    while artifact_id in used:
        artifact_id = f"{base}-{number}"
        number += 1
    used.add(artifact_id)
    return artifact_id


def _group_label(group: list[dict]) -> str:
    place_names = [item["place"] for item in group if item["place"]]
    if place_names:
        label = Counter(place_names).most_common(1)[0][0]
        return place_title(label) or label
    titles = [place_title(item["point_name"]) for item in group]
    titles = [title for title in titles if title]
    if titles:
        return Counter(titles).most_common(1)[0][0]
    return group[0]["point_name"]


def load_parishes() -> list[dict]:
    """Parish catalog. A second call returns the same rows and does not append."""
    global _parishes
    if _parishes is None:
        payload = json.loads(PARISHES_PATH.read_text(encoding="utf-8"))
        _parishes = list(payload["parishes"])
    return _parishes


def load_prikhody() -> list[dict]:
    """Prikhody catalog. A second call returns the same rows and does not append."""
    global _prikhody
    if _prikhody is None:
        if PRIKHODY_PATH.is_file():
            payload = json.loads(PRIKHODY_PATH.read_text(encoding="utf-8"))
            _prikhody = list(payload["parishes"])
        else:
            _prikhody = []
    return _prikhody


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _https(value: object) -> str:
    if isinstance(value, str) and value.startswith("https://"):
        return value
    return ""


def _record_count(value: object) -> int | str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _parish_coverage(parish: dict) -> list[dict]:
    rows = []
    for key in ("births", "marriages", "deaths"):
        part = parish.get(key)
        if not isinstance(part, dict):
            continue
        years = _text(part.get("years"))
        records = _record_count(part.get("records"))
        if records is None and not years and not _https(part.get("url")):
            continue
        rows.append(
            {
                "kind": key,
                "label": _ACT_LABEL[key],
                "years": years,
                "records": records,
            }
        )
    return rows


def _parish_url(parish: dict) -> str:
    url = _https(parish.get("url"))
    if url:
        return url
    for key in ("births", "marriages", "deaths"):
        part = parish.get(key)
        if isinstance(part, dict):
            url = _https(part.get("url"))
            if url:
                return url
    return ""


def _parish_point(parish: dict) -> tuple[float, float] | None:
    """One gazetteer point. Several candidates are not a coordinate."""
    gazetteer = parish.get("gazetteer")
    if not isinstance(gazetteer, dict):
        return None
    lat = gazetteer.get("lat")
    lon = gazetteer.get("lon")
    if isinstance(lat, bool) or isinstance(lon, bool):
        return None
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    return float(lat), float(lon)


def _verified_settlement_id(parish: dict) -> str:
    """The one settlement named by a verified match. Other statuses stay loose."""
    if parish.get("status") != "verified":
        return ""
    return _text(parish.get("settlement_id"))


def _listing(item: dict) -> dict:
    listed = {
        "id": item["id"],
        "name": item["name"],
        "emoji": item["emoji"],
        "label": item["label"],
        "indexed": item["indexed"],
    }
    if item.get("kind") == PARISH_KIND:
        listed["kind"] = PARISH_KIND
        listed["rite"] = item.get("rite") or ""
        listed["url"] = item.get("url") or ""
        listed["coverage"] = list(item.get("coverage") or [])
        listed["church"] = item.get("church") or ""
        listed["place"] = item.get("place") or ""
        listed["place_type"] = item.get("place_type") or ""
        listed["atd"] = list(item.get("atd") or [])
        listed["served"] = list(item.get("served") or [])
        listed["documents"] = list(item.get("documents") or [])
    return listed


def _name_key(value: str) -> frozenset[str]:
    return frozenset(part for part in fold(value).split() if part)


def _names_agree(left: str, right: str) -> bool:
    key = _name_key(left)
    return bool(key) and key == _name_key(right)


def _title_ok(value: str) -> bool:
    text = fold(value)
    if not text or text in _BARE_TITLES:
        return False
    if text.endswith("кладбище") or text.endswith("полевое"):
        return False
    return bool(place_title(value))


def _prikhody_settlement_title(parish: dict) -> str:
    place = _text(parish.get("place"))
    if _title_ok(place):
        return place
    for part in parish.get("atd") or []:
        if _title_ok(part):
            return part
    return place or "населённый пункт"


def _type_label(value: str) -> str:
    parts = value.split()
    if not parts or any(part.casefold() not in _ACT_LETTER for part in parts):
        return ""
    return ", ".join(_ACT_LETTER[part.casefold()] for part in parts)


def _document_view(document: dict) -> dict:
    shown = {
        "year": _text(document.get("year")),
        "type": _text(document.get("type")),
        "archive": _text(document.get("archive")),
        "archive_name": _text(document.get("archive_name")),
        "fod": _text(document.get("fod")),
        "url": _https(document.get("url")),
        "type_label": _type_label(_text(document.get("type"))),
        "digitized": document.get("digitized") is True,
        "rejected": document.get("rejected") is True,
    }
    return shown


def _prikhody_record(parish: dict) -> dict:
    documents = [_document_view(item) for item in parish.get("documents") or [] if isinstance(item, dict)]
    url = _https(parish.get("url"))
    status = FREE_ACCESS if documents or url else NO_INFO
    return {
        "id": "prikhody-" + _text(parish.get("id")),
        "name": _text(parish.get("name")) or _text(parish.get("place")) or _text(parish.get("id")),
        "emoji": status["emoji"],
        "label": status["label"],
        "indexed": False,
        "surnames": [],
        "kind": PARISH_KIND,
        "rite": _text(parish.get("rite")),
        "url": url if status is FREE_ACCESS else "",
        "coverage": [],
        "church": _text(parish.get("church")),
        "place": _text(parish.get("place")),
        "place_type": _text(parish.get("place_type")),
        "atd": [part for part in parish.get("atd") or [] if _text(part)],
        "served": [part for part in parish.get("served") or [] if _text(part)],
        "documents": documents,
        "settlement_id": "",
        "settlement_name": "",
    }


def _cell(lat: float, lon: float) -> tuple[int, int]:
    return (math.floor(lat / 0.05), math.floor(lon / 0.05))


def _nearby(cells: dict, lat: float, lon: float, limit_km: float) -> list[tuple[float, dict]]:
    cy, cx = _cell(lat, lon)
    found = []
    for dy in range(-6, 7):
        for dx in range(-6, 7):
            for settlement in cells.get((cy + dy, cx + dx), ()):
                distance = haversine_km(lat, lon, settlement["lat"], settlement["lon"])
                if distance <= limit_km:
                    found.append((distance, settlement))
    return found


def _matching_city(lat: float, lon: float, by_exact: dict) -> dict | None:
    city = _city_at(lat, lon)
    if city is None:
        return None
    options = [item for item in by_exact.get(city["name"], []) if item.get("lat") is not None]
    if not options:
        return None
    return min(options, key=lambda item: (haversine_km(lat, lon, item["lat"], item["lon"]), item["id"]))


def _closest(pairs: list[tuple[float, dict]]) -> dict:
    return min(pairs, key=lambda item: (item[0], item[1]["id"]))[1]


def _choose_settlement(lat: float, lon: float, place: str, cells: dict) -> dict | None:
    nearby = _nearby(cells, lat, lon, _NAME_KM)
    close = [item for item in nearby if item[0] <= _ATTACH_KM]
    named_close = [item for item in close if _names_agree(item[1]["name"], place)]
    if named_close:
        return _closest(named_close)
    if close:
        return _closest(close)
    named = [item for item in nearby if _names_agree(item[1]["name"], place)]
    if named:
        return _closest(named)
    return None


def _bind_parish(record: dict, settlement: dict, by_id: dict) -> None:
    settlement["artifacts"].append(_listing(record))
    record["settlement_id"] = settlement["id"]
    record["settlement_name"] = settlement["name"]
    by_id[record["id"]] = record


def _remember_settlement(settlement: dict, cells: dict, by_exact: dict, by_key: dict) -> None:
    cells[_cell(settlement["lat"], settlement["lon"])].append(settlement)
    by_exact[settlement["name"]].append(settlement)
    key = _name_key(settlement["name"])
    if key:
        by_key[key].append(settlement)


def _new_settlement(
    title: str,
    lat: float,
    lon: float,
    used: set[str],
    settlements: list[dict],
    cells: dict,
    by_exact: dict,
    by_key: dict,
) -> dict:
    settlement = {
        "id": _settlement_id(title, lat, lon, used),
        "name": title,
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "artifacts": [],
    }
    settlements.append(settlement)
    _remember_settlement(settlement, cells, by_exact, by_key)
    return settlement


def _attach_prikhody(settlements: list[dict], by_id: dict, unbound: list[dict]) -> None:
    """Attach every prikhody parish. Add a settlement when none is the same place."""
    used = {item["id"] for item in settlements}
    cells: dict = defaultdict(list)
    by_exact: dict = defaultdict(list)
    by_key: dict = defaultdict(list)
    for settlement in settlements:
        if settlement.get("lat") is None or settlement.get("lon") is None:
            continue
        _remember_settlement(settlement, cells, by_exact, by_key)
    seen: set[str] = set()
    located = []
    loose = []
    for parish in load_prikhody():
        raw_id = _text(parish.get("id"))
        if not raw_id or raw_id in seen:
            continue
        seen.add(raw_id)
        if "prikhody-" + raw_id in by_id:
            continue
        record = _prikhody_record(parish)
        lat = parish.get("lat")
        lon = parish.get("lon")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and not isinstance(lat, bool):
            located.append((parish, record, float(lat), float(lon)))
        else:
            loose.append((parish, record))
    located.sort(key=lambda item: item[1]["id"])
    for parish, record, lat, lon in located:
        chosen = _matching_city(lat, lon, by_exact) or _choose_settlement(lat, lon, record["place"], cells)
        if chosen is None:
            chosen = _new_settlement(
                _prikhody_settlement_title(parish),
                lat,
                lon,
                used,
                settlements,
                cells,
                by_exact,
                by_key,
            )
        _bind_parish(record, chosen, by_id)
    for parish, record in loose:
        matches = by_key.get(_name_key(record["place"])) or []
        if len(matches) == 1:
            _bind_parish(record, matches[0], by_id)
            continue
        by_id[record["id"]] = record
        unbound.append(
            {
                **_listing(record),
                "lat": None,
                "lon": None,
                "has_point": False,
                "suggestions": [],
            }
        )
    settlements.sort(key=lambda item: (fold(item["name"]), item["lat"], item["lon"]))
    for settlement in settlements:
        settlement["artifacts"].sort(key=lambda artifact: (fold(artifact["name"]), artifact["id"]))


def _attach_parishes(
    settlements: list[dict],
    by_id: dict,
    unbound: list[dict],
    aliases: dict[str, str] | None = None,
) -> None:
    by_settlement = {item["id"]: item for item in settlements}
    aliases = aliases or {}
    seen: set[str] = set()
    for parish in load_parishes():
        raw_id = _text(parish.get("id"))
        if not raw_id or raw_id in seen:
            continue
        seen.add(raw_id)
        artifact_id = "parish-" + raw_id
        if artifact_id in by_id:
            continue
        coverage = _parish_coverage(parish)
        status = FREE_ACCESS if coverage else NO_INFO
        record = {
            "id": artifact_id,
            "name": _text(parish.get("name")) or _text(parish.get("place")) or artifact_id,
            "emoji": status["emoji"],
            "label": status["label"],
            "indexed": False,
            "surnames": [],
            "kind": PARISH_KIND,
            "rite": _text(parish.get("rite")),
            "url": _parish_url(parish) if coverage else "",
            "coverage": coverage,
            "church": "",
            "place": "",
            "place_type": "",
            "atd": [],
            "served": [],
            "documents": [],
            "settlement_id": "",
            "settlement_name": "",
        }
        settlement_id = _verified_settlement_id(parish)
        settlement = by_settlement.get(settlement_id)
        if settlement is None and settlement_id:
            settlement = by_settlement.get(aliases.get(settlement_id, ""))
        if settlement is not None:
            settlement["artifacts"].append(_listing(record))
            record["settlement_id"] = settlement["id"]
            record["settlement_name"] = settlement["name"]
            by_id[artifact_id] = record
            continue
        by_id[artifact_id] = record
        point = _parish_point(parish)
        unbound.append(
            {
                **_listing(record),
                "lat": None if point is None else point[0],
                "lon": None if point is None else point[1],
                "has_point": point is not None,
                "suggestions": nearest_settlements(point[0], point[1], settlements) if point else [],
            }
        )
    for settlement in settlements:
        settlement["artifacts"].sort(key=lambda artifact: (fold(artifact["name"]), artifact["id"]))


def build_index() -> dict:
    payload = load_map()
    bundle = load_bundle()
    points = payload["points"]
    cemeteries = bundle["cemeteries"]
    surnames = _surnames_by_cemetery(bundle["burials"])
    places = _places_by_cemetery(bundle["burials"])
    matched = _match_cemeteries(points, cemeteries)
    matched_ids = {item["id"] for item in matched.values()}
    indexed_layer = next(
        (index for index, layer in enumerate(payload["layers"]) if layer.get("id") == "indexed"),
        None,
    )

    artifacts: list[dict] = []
    used_ids = set(matched_ids)
    for index, point in enumerate(points):
        cemetery = matched.get(index)
        lat = float(point[3])
        lon = float(point[2])
        point_name = point[1]
        indexed = cemetery is not None and _on_indexed_layer(point, indexed_layer)
        if cemetery is None:
            artifact_id = _map_id(point_name, lat, lon, used_ids)
            name = point_name
            place = ""
        else:
            artifact_id = cemetery["id"]
            name = cemetery["name"]
            place = places.get(artifact_id) or settlement_from_place(cemetery.get("place") or cemetery.get("np") or "")
        key = fold(place) if place else village_key(point_name)
        if key in _UNGROUPED:
            key = "#" + artifact_id
        artifacts.append(
            {
                "id": artifact_id,
                "name": name,
                "lat": lat,
                "lon": lon,
                "point_name": point_name,
                "place": place,
                "key": key,
                "indexed": indexed,
                "surnames": surnames.get(artifact_id, []) if indexed else [],
            }
        )

    for cemetery in cemeteries:
        if cemetery["id"] in matched_ids:
            continue
        artifact_id = cemetery["id"]
        place = places.get(artifact_id) or settlement_from_place(cemetery.get("place") or cemetery.get("np") or "")
        point_name = cemetery["name"]
        key = fold(place) if place else village_key(point_name)
        lat_raw = cemetery.get("lat")
        lon_raw = cemetery.get("lon")
        has_point = lat_raw is not None and lon_raw is not None
        if key in _UNGROUPED or not has_point:
            key = "#" + artifact_id
        artifacts.append(
            {
                "id": artifact_id,
                "name": point_name,
                "lat": float(lat_raw) if has_point else None,
                "lon": float(lon_raw) if has_point else None,
                "point_name": point_name,
                "place": place,
                "key": key,
                "indexed": False,
                "surnames": [],
            }
        )

    placeable = [artifact for artifact in artifacts if _can_place(artifact)]
    loose = [artifact for artifact in artifacts if not _can_place(artifact)]
    parent = list(range(len(placeable)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    by_key: dict[str, list[int]] = defaultdict(list)
    for index, artifact in enumerate(placeable):
        by_key[artifact["key"]].append(index)
    for indexes in by_key.values():
        if len(indexes) < 2:
            continue
        for left_at, left in enumerate(indexes):
            for right in indexes[left_at + 1 :]:
                one = placeable[left]
                other = placeable[right]
                if _km(one["lat"], one["lon"], other["lat"], other["lon"]) <= _MAX_KM:
                    union(left, right)

    groups: dict[int, list[dict]] = defaultdict(list)
    for index, artifact in enumerate(placeable):
        groups[find(index)].append(artifact)

    by_id: dict[str, dict] = {}
    used_settlement_ids: set[str] = set()
    prepared = []
    for group in groups.values():
        label = _group_label(group)
        lat = sum(item["lat"] for item in group) / len(group)
        lon = sum(item["lon"] for item in group) / len(group)
        prepared.append(
            {
                "id": _settlement_id(label, lat, lon, used_settlement_ids),
                "label": label,
                "lat": lat,
                "lon": lon,
                "group": group,
            }
        )

    aliases: dict[str, str] = {}
    kept = []
    buckets: dict[str, list] = defaultdict(list)
    for item in prepared:
        by_city: dict[str, list] = defaultdict(list)
        city_obj: dict[str, dict] = {}
        outside = []
        for artifact in item["group"]:
            found = _city_at(artifact["lat"], artifact["lon"])
            if found is None:
                outside.append(artifact)
            else:
                by_city[found["name"]].append(artifact)
                city_obj[found["name"]] = found
        if not by_city:
            kept.append(item)
            continue
        remnant = None
        if outside:
            remnant_label = _group_label(outside)
            remnant_lat = sum(artifact["lat"] for artifact in outside) / len(outside)
            remnant_lon = sum(artifact["lon"] for artifact in outside) / len(outside)
            remnant = {
                "id": _settlement_id(remnant_label, remnant_lat, remnant_lon, used_settlement_ids),
                "label": remnant_label,
                "lat": remnant_lat,
                "lon": remnant_lon,
                "group": outside,
            }
            kept.append(remnant)
        centroid_inside = _city_at(item["lat"], item["lon"]) is not None
        claimed = False
        for name, artifacts in by_city.items():
            source_id = None
            if not claimed:
                claimed = True
                if centroid_inside or remnant is None:
                    source_id = item["id"]
                else:
                    aliases[item["id"]] = remnant["id"]
            buckets[name].append((city_obj[name], artifacts, source_id))

    for name, rows in buckets.items():
        city = rows[0][0]
        artifacts = [artifact for _city, group, _source in rows for artifact in group]
        settlement_id = _settlement_id(name, city["lat"], city["lon"], used_settlement_ids)
        for _city, _group, source_id in rows:
            if source_id and source_id != settlement_id:
                aliases[source_id] = settlement_id
        kept.append(
            {
                "id": settlement_id,
                "label": name,
                "lat": city["lat"],
                "lon": city["lon"],
                "group": artifacts,
            }
        )

    settlements = []
    for item in kept:
        label = item["label"]
        settlement_id = item["id"]
        ordered = sorted(item["group"], key=lambda artifact: (fold(artifact["name"]), artifact["id"]))
        public_artifacts = []
        for artifact in ordered:
            status = FREE_ACCESS if artifact["indexed"] else NO_INFO
            record = {
                "id": artifact["id"],
                "name": artifact["name"],
                "emoji": status["emoji"],
                "label": status["label"],
                "indexed": artifact["indexed"],
                "surnames": artifact["surnames"],
                "settlement_id": settlement_id,
                "settlement_name": label,
            }
            by_id[artifact["id"]] = record
            public_artifacts.append(
                {
                    "id": artifact["id"],
                    "name": artifact["name"],
                    "emoji": status["emoji"],
                    "label": status["label"],
                    "indexed": artifact["indexed"],
                }
            )
        settlements.append(
            {
                "id": settlement_id,
                "name": label,
                "lat": round(item["lat"], 6),
                "lon": round(item["lon"], 6),
                "artifacts": public_artifacts,
            }
        )
    settlements.sort(key=lambda item: (fold(item["name"]), item["lat"], item["lon"]))
    unbound = []
    for item in loose:
        status = FREE_ACCESS if item["indexed"] else NO_INFO
        by_id[item["id"]] = {
            "id": item["id"],
            "name": item["name"],
            "emoji": status["emoji"],
            "label": status["label"],
            "indexed": item["indexed"],
            "surnames": item["surnames"],
            "settlement_id": "",
            "settlement_name": "",
        }
        has_point = item["lat"] is not None and item["lon"] is not None
        unbound.append(
            {
                "id": item["id"],
                "name": item["name"],
                "emoji": status["emoji"],
                "label": status["label"],
                "indexed": item["indexed"],
                "lat": item["lat"],
                "lon": item["lon"],
                "has_point": has_point,
                "suggestions": nearest_settlements(item["lat"], item["lon"], settlements) if has_point else [],
            }
        )
    _attach_parishes(settlements, by_id, unbound, aliases)
    _attach_prikhody(settlements, by_id, unbound)
    unbound.sort(key=lambda item: (fold(item["name"]), item["id"]))
    return {"settlements": settlements, "artifacts": by_id, "unbound": unbound, "aliases": aliases}


def load_index() -> dict:
    global _index
    if _index is None:
        _index = build_index()
    return _index


def apply_bindings(index: dict, bindings: dict[str, str]) -> dict:
    """Move bound artifacts onto their settlements. Does not mutate the cache."""
    if not bindings:
        return index
    settlements = []
    by_settlement = {}
    for item in index["settlements"]:
        copy = {**item, "artifacts": list(item["artifacts"])}
        settlements.append(copy)
        by_settlement[copy["id"]] = copy
    artifacts = dict(index["artifacts"])
    aliases = index.get("aliases") or {}
    unbound = []
    for item in index["unbound"]:
        wanted = bindings.get(item["id"], "")
        settlement = by_settlement.get(wanted)
        if settlement is None and wanted:
            settlement = by_settlement.get(aliases.get(wanted, ""))
        if settlement is None:
            unbound.append(item)
            continue
        settlement["artifacts"].append(_listing(item))
        settlement["artifacts"].sort(key=lambda artifact: (fold(artifact["name"]), artifact["id"]))
        record = dict(artifacts[item["id"]])
        record["settlement_id"] = settlement["id"]
        record["settlement_name"] = settlement["name"]
        artifacts[item["id"]] = record
    return {"settlements": settlements, "unbound": unbound, "artifacts": artifacts}


def _view(bindings: dict[str, str] | None = None) -> dict:
    return apply_bindings(load_index(), bindings or {})


def has_information(artifact: dict) -> bool:
    """True for every status except «Нет информации»."""
    return artifact.get("label") != NO_INFO["label"]


def marker_tone(artifacts: list[dict]) -> str:
    """0% is red, strictly below 25% is yellow, 25% and above is green."""
    informed = sum(1 for item in artifacts if has_information(item))
    if informed == 0:
        return "red"
    if informed * 4 < len(artifacts):
        return "yellow"
    return "green"


def marker_for(artifacts: list[dict]) -> dict:
    tone = marker_tone(artifacts)
    return {"marker_class": _MARKER_CLASS[tone], "marker_label": _MARKER_LABEL[tone]}


def public_map(bindings: dict[str, str] | None = None) -> dict:
    settlements = []
    for item in _view(bindings)["settlements"]:
        settlements.append({**item, **marker_for(item["artifacts"])})
    return {"settlements": settlements}


def unbound_artifacts(bindings: dict[str, str] | None = None) -> list[dict]:
    return _view(bindings)["unbound"]


def get_artifact(artifact_id: str, bindings: dict[str, str] | None = None) -> dict | None:
    return _view(bindings)["artifacts"].get(artifact_id)


def saved_bindings() -> list[dict]:
    if not BINDINGS_PATH.is_file():
        return []
    return json.loads(BINDINGS_PATH.read_text(encoding="utf-8"))


def auto_bindings(index: dict | None = None) -> list[dict]:
    """Nearest settlement for every unbound cemetery that has coordinates."""
    index = index or load_index()
    rows = []
    for item in index["unbound"]:
        if not item["has_point"]:
            continue
        found = nearest_settlements(item["lat"], item["lon"], index["settlements"], limit=1)
        if not found:
            continue
        nearest = found[0]
        rows.append(
            {
                "artifact_id": item["id"],
                "settlement_id": nearest["id"],
                "km": nearest["km"],
            }
        )
    rows.sort(key=lambda row: row["artifact_id"])
    return rows


def ensure_artifact_settlements(db) -> None:
    """Insert file bindings. A row ruslan already saved is left as it is."""
    from sqlalchemy import select

    from app.models import ArtifactSettlement

    rows = saved_bindings()
    if not rows:
        return
    seen = set(db.scalars(select(ArtifactSettlement.artifact_id)))
    pending = []
    for row in rows:
        artifact_id = row["artifact_id"]
        if artifact_id in seen:
            continue
        seen.add(artifact_id)
        pending.append(ArtifactSettlement(artifact_id=artifact_id, settlement_id=row["settlement_id"]))
    if not pending:
        return
    db.add_all(pending)
    db.commit()
