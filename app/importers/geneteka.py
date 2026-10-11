from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx

from app.importers.tables import MAX_ROWS

HOST = "geneteka.genealodzy.pl"
PAGE_SIZE = 50
MAX_PAGES = 2000
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# Site tabs: B urodzenia, S śluby, D zgony. Marriage is S, not M.
EVENT_LABEL = {"B": "рождение", "S": "брак", "D": "смерть"}
EVENT_ORDER = ("B", "S", "D")
TABLE_EVENT = {"table_b": "B", "table_s": "S", "table_d": "D"}
MISSING = {
    "B": "Индекса рождений на странице нет.",
    "S": "Индекса браков на странице нет.",
    "D": "Индекса смертей на странице нет.",
}
FAILED = {
    "B": "Не удалось скачать рождения.",
    "S": "Не удалось скачать браки.",
    "D": "Не удалось скачать смерти.",
}
TRUNCATED = {
    "B": "Рождения скачаны не полностью.",
    "S": "Браки скачаны не полностью.",
    "D": "Смерти скачаны не полностью.",
}

COLUMNS = (
    "Событие",
    "Год",
    "Акт",
    "Имя",
    "Фамилия",
    "Имя отца",
    "Имя матери",
    "Фамилия матери",
    "Родители",
    "Имя супруга",
    "Фамилия супруга",
    "Родители супруга",
    "Приход",
    "Место",
    "Примечания",
    "Адрес",
    "Фио",
    "Год рождения",
)

_W = re.compile(r"^[0-9A-Za-z]{1,16}$")
_RID = re.compile(r"^[0-9]{1,10}$")
_SKIP_TITLE = re.compile(r"^(zgłoś poprawkę|zglos poprawke)$", re.I)
_INDEXER = re.compile(r"^indeks doda", re.I)


class GenetekaError(Exception):
    pass


@dataclass
class GenetekaLoad:
    parish: str
    w: str
    anchor: str
    rows: list[dict[str, str]]
    warnings: list[str] = field(default_factory=list)

    @property
    def filename(self) -> str:
        return dataset_filename(self.parish, self.w, self.anchor)

    def counts(self) -> dict[str, int]:
        totals = {"рождение": 0, "брак": 0, "смерть": 0}
        for row in self.rows:
            label = row.get("Событие", "")
            if label in totals:
                totals[label] += 1
        return totals

    def message(self) -> str:
        totals = self.counts()
        parts = []
        if self.parish:
            parts.append(f"Приход {self.parish}.")
        parts.append(
            f"Рождения — {totals['рождение']}, браки — {totals['брак']}, смерти — {totals['смерть']}."
        )
        if self.warnings:
            parts.append(" ".join(self.warnings))
        return " ".join(parts)

    def as_payload(self) -> dict:
        return {
            "parish": self.parish,
            "w": self.w,
            "anchor": self.anchor,
            "warnings": self.warnings,
            "rows": self.rows,
        }


def dataset_filename(parish: str, w: str, anchor: str) -> str:
    label = " ".join((parish or "приход").replace("/", " ").replace("\\", " ").split())
    return f"Geneteka {label[:80]} {w}-{anchor}.csv"[:255]


def local_hint(space_id: int) -> str:
    return (
        "Если сервер не открывает сайт, на своём компьютере выполните "
        f"python -m app.tools.geneteka «адрес» --upload https://62.238.112.84 --space {space_id} --user ИМЯ. "
        "Пароль программа спросит сама."
    )


def parse_parish_url(url: str) -> dict[str, str]:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or host != HOST:
        raise ValueError("Нужна ссылка на geneteka.genealodzy.pl.")
    query = parse_qs(parsed.query)
    op = (query.get("op") or ["gt"])[0]
    if op != "gt":
        raise ValueError("Нужна ссылка на таблицу прихода Geneteka.")
    w = (query.get("w") or [""])[0]
    rid = (query.get("rid") or [""])[0]
    bdm = (query.get("bdm") or ["B"])[0].upper()
    if bdm == "M":
        bdm = "S"
    if _W.fullmatch(w) is None or _RID.fullmatch(rid) is None or bdm not in EVENT_LABEL:
        raise ValueError("В ссылке нет региона или номера прихода.")
    return {"w": w, "rid": rid, "bdm": bdm, "url": url.strip()}


def _ws(value: str) -> str:
    return " ".join(value.replace("\x0b", " ").replace("\r", " ").replace("\n", " ").split())


def _title_parts(title: str) -> list[str]:
    parts = []
    for chunk in re.split(r"[\x0b\r\n]+", title):
        text = _ws(chunk)
        if text:
            parts.append(text)
    return parts


def _apply_titles(text: str, titles: list[str]) -> tuple[str, list[str], str]:
    notes: list[str] = []
    place = ""
    extras: list[str] = []
    for title in titles:
        for part in _title_parts(title):
            folded = part.casefold()
            if _SKIP_TITLE.match(folded) or _INDEXER.match(folded):
                continue
            if folded.startswith("inne nazwiska"):
                value = part.split(":", 1)[1].strip() if ":" in part else part
                if value:
                    extras.append(value)
                continue
            if folded.startswith("miejscowość") or folded.startswith("miejscowosc"):
                value = part.split(":", 1)[1].strip() if ":" in part else ""
                if value and not place:
                    place = value
                continue
            notes.append(part)
    if extras:
        text = f"{text} ({'; '.join(extras)})".strip()
    return text, notes, place


class _CellParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.titles: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        title = dict(attrs).get("title")
        if title:
            self.titles.append(title)

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


def clean_value(value: object) -> tuple[str, list[str]]:
    if value is None:
        return "", []
    if not isinstance(value, str):
        return _ws(str(value)), []
    if "<" not in value:
        return _ws(value), []
    parser = _CellParser()
    parser.feed(value)
    parser.close()
    return _ws(" ".join(parser.parts)), parser.titles


def header_roles(headers: list[str]) -> list[str]:
    roles: list[str] = []
    given = surname = parents = 0
    for header in headers:
        key = header.casefold().strip()
        if key == "rok":
            roles.append("year")
        elif key == "akt":
            roles.append("act")
        elif key == "imię":
            given += 1
            roles.append("given" if given == 1 else "spouse_given")
        elif key == "nazwisko":
            surname += 1
            roles.append("surname" if surname == 1 else "spouse_surname")
        elif key == "imię ojca":
            roles.append("father")
        elif key == "imię matki":
            roles.append("mother")
        elif key == "nazwisko matki":
            roles.append("mother_surname")
        elif key == "rodzice":
            parents += 1
            roles.append("parents" if parents == 1 else "spouse_parents")
        elif key == "parafia":
            roles.append("parish")
        elif key == "miejscowość":
            roles.append("place")
        elif key == "uwagi":
            roles.append("notes")
        else:
            roles.append("extra")
    return roles


def build_row(event: str, headers: list[str], cells: list[object], source_url: str) -> dict[str, str]:
    roles = header_roles(headers)
    values = {role: "" for role in (
        "year", "act", "given", "surname", "father", "mother", "mother_surname",
        "parents", "spouse_given", "spouse_surname", "spouse_parents", "parish", "place",
    )}
    notes: list[str] = []
    place_from_title = ""
    for index, cell in enumerate(cells):
        if isinstance(cell, tuple):
            text, titles = cell
        else:
            text, titles = clean_value(cell)
        text, extra_notes, found_place = _apply_titles(text, titles)
        role = roles[index] if index < len(roles) else "extra"
        if role == "extra":
            if text:
                notes.append(text)
        elif role == "notes":
            if text:
                notes.append(text)
        else:
            values[role] = text
        notes.extend(extra_notes)
        if found_place and not place_from_title:
            place_from_title = found_place
    if not values["place"] and place_from_title:
        values["place"] = place_from_title
    label = EVENT_LABEL[event]
    fio = " ".join(part for part in (values["given"], values["surname"]) if part)
    if not fio:
        fio = " ".join(part for part in (values["spouse_given"], values["spouse_surname"]) if part)
    row = {column: "" for column in COLUMNS}
    row.update(
        {
            "Событие": label,
            "Год": values["year"],
            "Акт": values["act"],
            "Имя": values["given"],
            "Фамилия": values["surname"],
            "Имя отца": values["father"],
            "Имя матери": values["mother"],
            "Фамилия матери": values["mother_surname"],
            "Родители": values["parents"],
            "Имя супруга": values["spouse_given"],
            "Фамилия супруга": values["spouse_surname"],
            "Родители супруга": values["spouse_parents"],
            "Приход": values["parish"],
            "Место": values["place"],
            "Примечания": "; ".join(notes),
            "Адрес": source_url,
            "Фио": fio[:500],
            "Год рождения": values["year"] if event == "B" else "",
        }
    )
    return row


def ensure_row(raw: dict) -> dict[str, str]:
    row = {column: "" for column in COLUMNS}
    for column in COLUMNS:
        value = raw.get(column, "")
        if value is None:
            value = ""
        row[column] = _ws(str(value))[:5000]
    if not row["Фио"]:
        row["Фио"] = " ".join(part for part in (row["Имя"], row["Фамилия"]) if part)[:500]
    if not row["Фио"]:
        row["Фио"] = " ".join(part for part in (row["Имя супруга"], row["Фамилия супруга"]) if part)[:500]
    if row["Событие"] == "рождение":
        if not row["Год рождения"]:
            row["Год рождения"] = row["Год"]
    else:
        row["Год рождения"] = ""
    return row


def _dedupe(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    kept: list[dict[str, str]] = []
    seen: set[tuple[str, ...]] = set()
    for row in rows:
        if not row.get("Фио"):
            continue
        key = tuple(row[column] for column in COLUMNS)
        if key in seen:
            continue
        seen.add(key)
        kept.append(row)
    return kept


def _finish(parish: str, w: str, anchor: str, rows: list[dict[str, str]], warnings: list[str], seen: set[str], failed: set[str]) -> GenetekaLoad:
    if len(rows) > MAX_ROWS:
        raise ValueError("В таблице больше 200 000 строк.")
    for code in EVENT_ORDER:
        if code in failed or code in seen:
            continue
        warnings.append(MISSING[code])
    unique = _dedupe(rows)
    if not parish:
        for row in unique:
            if row["Приход"]:
                parish = row["Приход"]
                break
    return GenetekaLoad(parish=parish, w=w, anchor=anchor, rows=unique, warnings=warnings)


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tabs: list[dict[str, str]] = []
        self.tables: dict[str, dict[str, list]] = {}
        self.next_href = ""
        self._skip = 0
        self._li: dict[str, str] | None = None
        self._link: str | None = None
        self._link_text: list[str] = []
        self._table_id = ""
        self._depth = 0
        self._in_cell = False
        self._cell_text: list[str] = []
        self._cell_titles: list[str] = []
        self._row: list[tuple[str, list[str]]] = []
        self._header = False
        self._headers: list[str] = []
        self._body: list[list[tuple[str, list[str]]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip += 1
            return
        if self._skip:
            return
        attr = {key: value or "" for key, value in attrs}
        if tag == "li" and attr.get("data-key") and attr.get("data-rid"):
            self._li = {"key": attr["data-key"], "rid": attr["data-rid"], "href": ""}
        if tag == "a":
            self._link = attr.get("href", "")
            self._link_text = []
            if self._li is not None and not self._li["href"]:
                self._li["href"] = self._link
        if tag == "table" and attr.get("id", "").startswith("table_") and not self._table_id:
            self._table_id = attr["id"]
            self._depth = 1
            self._headers = []
            self._body = []
            return
        if tag == "table" and self._table_id:
            self._depth += 1
            return
        if self._table_id and tag == "tr":
            self._row = []
            self._header = False
        if self._table_id and tag in {"td", "th"}:
            self._in_cell = True
            self._cell_text = []
            self._cell_titles = []
            if tag == "th":
                self._header = True
        if self._in_cell and attr.get("title"):
            self._cell_titles.append(attr["title"])

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1
            return
        if self._skip:
            return
        if tag == "a" and self._link is not None:
            text = _ws(" ".join(self._link_text))
            if text.casefold() in {"następna", "nastepna"} and not self.next_href:
                self.next_href = self._link
            self._link = None
        if tag == "li" and self._li is not None:
            self.tabs.append(self._li)
            self._li = None
        if self._table_id and tag in {"td", "th"} and self._in_cell:
            self._row.append((_ws(" ".join(self._cell_text)), list(self._cell_titles)))
            self._in_cell = False
        if self._table_id and tag == "tr":
            if self._header and not self._headers:
                self._headers = [cell[0] for cell in self._row]
            elif any(cell[0] or cell[1] for cell in self._row):
                self._body.append(self._row)
            self._row = []
        if tag == "table" and self._table_id:
            self._depth -= 1
            if self._depth <= 0:
                self.tables[self._table_id] = {"headers": self._headers, "rows": self._body}
                self._table_id = ""

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._link is not None:
            self._link_text.append(data)
        if self._in_cell:
            self._cell_text.append(data)


def parse_html(html: str) -> _PageParser:
    parser = _PageParser()
    parser.feed(html)
    parser.close()
    return parser


def _query(url: str) -> dict[str, str]:
    parsed = parse_qs(urlparse(url).query)
    bdm = (parsed.get("bdm") or [""])[0].upper()
    if bdm == "M":
        bdm = "S"
    return {
        "w": (parsed.get("w") or [""])[0],
        "rid": (parsed.get("rid") or [""])[0],
        "bdm": bdm,
    }


def _same_index(left: str, right: str) -> bool:
    a = _query(left)
    b = _query(right)
    return bool(a["rid"]) and a == b


def index_url(w: str, bdm: str, rid: str) -> str:
    query = urlencode(
        {
            "op": "gt",
            "lang": "pol",
            "bdm": bdm,
            "w": w,
            "rid": rid,
            "search_lastname": "",
            "search_name": "",
            "search_lastname2": "",
            "search_name2": "",
            "from_date": "",
            "to_date": "",
            "rpp1": "",
            "ordertable": "",
        }
    )
    return f"https://{HOST}/index.php?{query}"


def act_url(w: str, bdm: str, rid: str, column_count: int, start: int, length: int) -> str:
    params: list[tuple[str, str]] = [("draw", "1")]
    for index in range(column_count):
        last = index == column_count - 1
        flag = "false" if last else "true"
        params.extend(
            (
                (f"columns[{index}][data]", str(index)),
                (f"columns[{index}][name]", ""),
                (f"columns[{index}][searchable]", flag),
                (f"columns[{index}][orderable]", flag),
                (f"columns[{index}][search][value]", ""),
                (f"columns[{index}][search][regex]", "false"),
            )
        )
    for index in range(min(3, column_count)):
        params.extend(((f"order[{index}][column]", str(index)), (f"order[{index}][dir]", "asc")))
    params.extend(
        (
            ("start", str(start)),
            ("length", str(length)),
            ("search[value]", ""),
            ("search[regex]", "false"),
            ("op", "gt"),
            ("lang", "pol"),
            ("bdm", bdm),
            ("w", w),
            ("rid", rid),
            ("search_lastname", ""),
            ("search_name", ""),
            ("search_lastname2", ""),
            ("search_name2", ""),
            ("from_date", ""),
            ("to_date", ""),
            ("rpp1", ""),
            ("ordertable", ""),
        )
    )
    return f"https://{HOST}/api/getAct.php?{urlencode(params)}"


def _parish_name(payload: dict) -> str:
    parishes = payload.get("parishes")
    if not isinstance(parishes, dict):
        return ""
    for group in parishes.values():
        if isinstance(group, list) and group and isinstance(group[0], list) and len(group[0]) > 1:
            return _ws(str(group[0][1]))
    return ""


def _host_ok(url: str) -> bool:
    return (urlparse(url).hostname or "").casefold() == HOST


def load_parish(url: str, get, *, page_size: int = PAGE_SIZE, pause: float = 0.35, max_pages: int = MAX_PAGES) -> GenetekaLoad:
    """Download every births, marriages and deaths page for one parish.

    The HTML table is empty. Rows come from DataTables at api/getAct.php:
    start is the offset, length is the page size. The request needs the
    header X-Requested-With: XMLHttpRequest, otherwise the API returns a
    short unrelated slice. get(url, ajax=False) performs one GET.
    """
    spec = parse_parish_url(url)
    calls = {"n": 0}

    def paced(target: str, *, ajax: bool = False) -> str:
        if not _host_ok(target):
            raise GenetekaError("Нужна ссылка на geneteka.genealodzy.pl.")
        if calls["n"] and pause:
            time.sleep(pause)
        calls["n"] += 1
        return get(target, ajax=ajax)

    first_html = paced(spec["url"])
    parsed = parse_html(first_html)
    if any(table["rows"] for table in parsed.tables.values()) or parsed.next_href:
        return _load_html(spec, parsed, paced, max_pages)
    return _load_json(spec, parsed, paced, page_size, max_pages)


def _load_html(spec: dict[str, str], first: _PageParser, paced, max_pages: int) -> GenetekaLoad:
    rows: list[dict[str, str]] = []
    seen_events: set[str] = set()
    seen_urls = {spec["url"]}
    current_url = spec["url"]
    current = first
    pages = 0
    while pages < max_pages:
        pages += 1
        for table_id, table in current.tables.items():
            event = TABLE_EVENT.get(table_id)
            if event is None or not table["rows"]:
                continue
            seen_events.add(event)
            for cells in table["rows"]:
                rows.append(build_row(event, table["headers"], cells, current_url))
        href = current.next_href
        if not href:
            break
        nxt = urljoin(current_url, href)
        if not _host_ok(nxt) or nxt in seen_urls or pages >= max_pages:
            break
        seen_urls.add(nxt)
        current_url = nxt
        current = parse_html(paced(nxt))
    anchor = spec["rid"]
    return _finish(spec.get("parish", ""), spec["w"], anchor, rows, [], seen_events, set())


def _tabs(spec: dict[str, str], parsed: _PageParser, page_url: str) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for tab in parsed.tabs:
        href = urljoin(page_url, tab["href"]) if tab.get("href") else ""
        query = _query(href) if href else {"w": spec["w"], "rid": tab["rid"], "bdm": tab["key"].upper()}
        bdm = query["bdm"] or tab["key"].upper()
        if bdm == "M":
            bdm = "S"
        rid = query["rid"] or tab["rid"]
        w = query["w"] or spec["w"]
        if bdm not in EVENT_LABEL or _RID.fullmatch(rid) is None or _W.fullmatch(w) is None:
            continue
        found.append({"bdm": bdm, "rid": rid, "w": w, "url": href or index_url(w, bdm, rid)})
    if not found:
        found.append({"bdm": spec["bdm"], "rid": spec["rid"], "w": spec["w"], "url": spec["url"]})
    found.sort(key=lambda item: EVENT_ORDER.index(item["bdm"]) if item["bdm"] in EVENT_ORDER else 9)
    unique: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in found:
        if item["bdm"] in seen:
            continue
        seen.add(item["bdm"])
        unique.append(item)
    return unique


def _load_json(spec: dict[str, str], parsed: _PageParser, paced, page_size: int, max_pages: int) -> GenetekaLoad:
    rows: list[dict[str, str]] = []
    warnings: list[str] = []
    seen: set[str] = set()
    failed: set[str] = set()
    parish = ""
    documents = {spec["url"]: parsed}
    for tab in _tabs(spec, parsed, spec["url"]):
        event = tab["bdm"]
        try:
            document = documents.get(tab["url"])
            if document is None and _same_index(tab["url"], spec["url"]):
                document = parsed
            if document is None:
                document = parse_html(paced(tab["url"]))
                documents[tab["url"]] = document
            table_id = {value: key for key, value in TABLE_EVENT.items()}[event]
            table = document.tables.get(table_id) or next(iter(document.tables.values()), None)
            headers = table["headers"] if table else []
            if not headers:
                failed.add(event)
                warnings.append(FAILED[event])
                continue
            source = index_url(tab["w"], event, tab["rid"])
            page_rows, name, problem = _pages(tab, headers, source, paced, page_size, max_pages)
        except (GenetekaError, OSError, ValueError, json.JSONDecodeError):
            failed.add(event)
            warnings.append(FAILED[event])
            continue
        seen.add(event)
        if name and not parish:
            parish = name
        rows.extend(page_rows)
        if problem:
            warnings.append(problem)
    anchor = spec["rid"]
    for tab in _tabs(spec, parsed, spec["url"]):
        if tab["bdm"] == "B":
            anchor = tab["rid"]
            break
    return _finish(parish, spec["w"], anchor, rows, warnings, seen, failed)


def _pages(tab: dict[str, str], headers: list[str], source: str, paced, page_size: int, max_pages: int):
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, ...]] = set()
    parish = ""
    start = 0
    pages = 0
    problem = ""
    event = tab["bdm"]
    while pages < max_pages:
        pages += 1
        body = paced(act_url(tab["w"], event, tab["rid"], len(headers), start, page_size), ajax=True)
        payload = json.loads(body)
        data = payload.get("data")
        if not isinstance(data, list):
            raise ValueError("Пустой ответ Geneteka.")
        if not parish:
            parish = _parish_name(payload)
        added = 0
        for cells in data:
            if not isinstance(cells, list):
                continue
            row = build_row(event, headers, cells, source)
            if not row["Фио"]:
                continue
            key = tuple(row[column] for column in COLUMNS)
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
            added += 1
        total = payload.get("recordsFiltered", payload.get("recordsTotal"))
        if total is None:
            total = start + len(data)
        total = int(total)
        if not data:
            break
        start += page_size
        if start >= total:
            break
        if added == 0:
            problem = TRUNCATED[event]
            break
    else:
        problem = TRUNCATED[event]
    return rows, parish, problem


def fetch_parish(url: str, *, page_size: int = PAGE_SIZE, pause: float = 0.35) -> GenetekaLoad:
    timeout = httpx.Timeout(40.0, connect=15.0)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        def get(target: str, ajax: bool = False) -> str:
            headers = {"User-Agent": USER_AGENT, "Referer": f"https://{HOST}/"}
            if ajax:
                headers["X-Requested-With"] = "XMLHttpRequest"
                headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
            try:
                response = client.get(target, headers=headers)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise GenetekaError("Не удалось открыть Geneteka.") from exc
            return response.text

        return load_parish(url, get, page_size=page_size, pause=pause)


def load_from_payload(payload: object) -> GenetekaLoad:
    if not isinstance(payload, dict):
        raise ValueError("Некорректный индекс.")
    w = str(payload.get("w") or "")
    anchor = str(payload.get("anchor") or "")
    parish = _ws(str(payload.get("parish") or ""))
    if _W.fullmatch(w) is None or _RID.fullmatch(anchor) is None:
        raise ValueError("Некорректный индекс прихода.")
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ValueError("В индексе нет строк.")
    if len(raw_rows) > MAX_ROWS:
        raise ValueError("В таблице больше 200 000 строк.")
    rows = []
    for item in raw_rows:
        if isinstance(item, dict):
            rows.append(ensure_row(item))
    warnings = []
    raw_warnings = payload.get("warnings") or []
    if isinstance(raw_warnings, list):
        for item in raw_warnings:
            if isinstance(item, str) and item.strip():
                warnings.append(item.strip()[:300])
    loaded = _finish(parish, w, anchor, rows, warnings, set(EVENT_LABEL), set())
    if not loaded.rows:
        raise ValueError("В индексе нет строк.")
    return loaded
