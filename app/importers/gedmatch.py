from __future__ import annotations

import re
from html.parser import HTMLParser

from app.importers.tables import parse_float

_KIT_OWNER = re.compile(
    r"Kit:\s*([A-Za-z0-9]+)\s*\(([^()]*)\)\s*\[([^\[\]]*)\]",
    re.IGNORECASE,
)

HEADER_FIELDS = {
    "kit": "kit",
    "kit nbr": "kit",
    "kit number": "kit",
    "name": "name",
    "largest seg": "largest",
    "largest segment": "largest",
    "total cm": "total",
    "totalcm": "total",
    "gen": "gen",
    "overlap": "overlap",
    "date compared": "compared",
    "source": "company",
    "testing company": "company",
    "company": "company",
}


class GedMatchError(Exception):
    pass


class _Tables(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._rows: list[list[str]] | None = None
        self._cells: list[str] | None = None
        self._text: list[str] = []
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "table":
            self._rows = []
        elif tag == "tr" and self._rows is not None:
            self._cells = []
        elif tag in {"td", "th"} and self._cells is not None:
            self._in_cell = True
            self._text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._in_cell and self._cells is not None:
            self._cells.append(" ".join("".join(self._text).split()))
            self._in_cell = False
        elif tag == "tr" and self._rows is not None and self._cells is not None:
            if any(self._cells):
                self._rows.append(self._cells)
            self._cells = None
        elif tag == "table" and self._rows is not None:
            self.tables.append(self._rows)
            self._rows = None

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._text.append(data)


def _field(header: str) -> str:
    return HEADER_FIELDS.get(" ".join(header.casefold().split()), "")


def parse_kit_owner(html: str) -> dict | None:
    match = _KIT_OWNER.search(html)
    if match is None:
        return None
    kit, inside, company = match.group(1), match.group(2).strip(), match.group(3).strip()
    name, email = inside, ""
    if "," in inside:
        name, maybe_email = inside.rsplit(",", 1)
        maybe_email = maybe_email.strip()
        if "@" in maybe_email:
            email = maybe_email
            name = name.strip()
    return {
        "kit": kit.strip().upper(),
        "name": " ".join(name.split()),
        "email": email,
        "company": " ".join(company.split()),
    }


def parse_one_to_many(html: str) -> list[dict]:
    parser = _Tables()
    parser.feed(html)
    chosen: list[list[str]] | None = None
    fields: list[str] = []
    for table in parser.tables:
        if not table:
            continue
        mapped = [_field(cell) for cell in table[0]]
        if "kit" in mapped and "total" in mapped and mapped.count("kit") == 1:
            chosen = table
            fields = mapped
    if chosen is None:
        raise GedMatchError("В странице нет таблицы one-to-many.")
    rows = []
    seen: set[str] = set()
    for cells in chosen[1:]:
        record = {field: cells[index] if index < len(cells) else "" for index, field in enumerate(fields) if field}
        kit = record.get("kit", "").strip().upper()
        if not kit or kit in seen:
            continue
        seen.add(kit)
        rows.append(
            {
                "kit": kit,
                "name": record.get("name", "").strip(),
                "total_cm": parse_float(record.get("total") or ""),
                "largest_cm": parse_float(record.get("largest") or ""),
                "overlap": parse_float(record.get("overlap") or ""),
                "generations": parse_float(record.get("gen") or ""),
                "date_compared": record.get("compared", "").strip(),
                "company": record.get("company", "").strip(),
            }
        )
    return rows


def rank_kit(kits: list[str], own_kit: str, rank: int) -> str | None:
    own = own_kit.strip().upper()
    filtered = [kit.strip().upper() for kit in kits if kit.strip() and kit.strip().upper() != own]
    if rank < 1 or rank > len(filtered):
        return None
    return filtered[rank - 1]


def walk_rank(
    pages: dict[str, list[str]], start: str, rank: int, saved: set[str]
) -> tuple[list[str], list[str], list[str]]:
    """Follow rank-N matches. Stop when the next page is already saved. Do not open a kit twice."""
    current = start.strip().upper()
    order: list[str] = []
    opened: list[str] = []
    skipped: list[str] = []
    seen: set[str] = set()
    while current and current not in seen:
        if current not in pages:
            break
        seen.add(current)
        order.append(current)
        if current in saved:
            skipped.append(current)
        else:
            saved.add(current)
            opened.append(current)
        nxt = rank_kit(pages[current], current, rank)
        if nxt is None or nxt in saved:
            break
        current = nxt
    return order, opened, skipped
