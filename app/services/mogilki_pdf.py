"""Read the Novogrudok burial-list PDFs into rows.

Columns are positional. A text line that crosses a column boundary is split
by character, so "19 12 1984" becomes day, month and year.
"""

from __future__ import annotations

import re
from pathlib import Path

COLUMNS = (
    ("number", 0, 66),
    ("cemetery", 66, 168),
    ("surname", 168, 255),
    ("given", 255, 328),
    ("patronymic", 328, 405),
    ("bday", 405, 428),
    ("bmonth", 428, 445),
    ("byear", 445, 472),
    ("age", 472, 490),
    ("dday", 490, 510),
    ("dmonth", 510, 526),
    ("dyear", 526, 575),
)

_HEADER = {
    "номер2",
    "кладбище",
    "фамилия",
    "имя",
    "отчество",
    "род",
    "ром",
    "рог",
    "возр",
    "смд",
    "смм",
    "смг",
}
_YEAR = re.compile(r"^(\d{4})\??$")
_NUM = re.compile(r"^(\d{1,4})\??$")


def _column(x: float) -> str | None:
    for name, start, end in COLUMNS:
        if start <= x < end:
            return name
    return None


def _pieces(line) -> dict[str, str]:
    from pdfminer.layout import LTChar

    text = line.get_text().replace("\n", " ").strip()
    if not text:
        return {}
    start = _column(line.x0)
    end = _column(max(line.x0, line.x1 - 0.1))
    if start and start == end:
        return {start: text}
    buckets: dict[str, list[tuple[float, str]]] = {}
    for char in line:
        if not isinstance(char, LTChar):
            continue
        raw = char.get_text()
        if not raw or raw == "\n":
            continue
        name = _column(char.x0)
        if name is None:
            continue
        buckets.setdefault(name, []).append((char.x0, raw))
    pieces = {}
    for name, chars in buckets.items():
        chars.sort(key=lambda item: item[0])
        chunk = []
        previous = None
        for x, raw in chars:
            if previous is not None and x - previous > 14 and chunk and chunk[-1] != " ":
                chunk.append(" ")
            chunk.append(raw)
            if raw.strip():
                previous = x
        value = re.sub(r"\s+", " ", "".join(chunk)).strip()
        if value:
            pieces[name] = value
    return pieces


def _rows(page) -> list[dict[str, str]]:
    from pdfminer.layout import LTTextLine

    lines = []

    def walk(element) -> None:
        if isinstance(element, LTTextLine):
            lines.append(element)
        elif hasattr(element, "__iter__"):
            for child in element:
                walk(child)

    walk(page)
    kept = []
    for line in lines:
        if line.y0 > 800:
            continue
        text = line.get_text().strip()
        if not text or "telegram" in text.lower() or text.lower().startswith("новогрудский район"):
            continue
        if re.fullmatch(r"\d+\s+из\s+\d+", text):
            continue
        kept.append(line)
    kept.sort(key=lambda line: line.y0, reverse=True)
    grouped: list[tuple[float, list]] = []
    for line in kept:
        if grouped and abs(grouped[-1][0] - line.y0) <= 1.5:
            grouped[-1][1].append(line)
        else:
            grouped.append((line.y0, [line]))
    rows = []
    for _y, members in grouped:
        fields: dict[str, str] = {}
        for line in members:
            for key, value in _pieces(line).items():
                if key in fields:
                    fields[key] = (fields[key] + " " + value).strip()
                else:
                    fields[key] = value
        if not fields:
            continue
        label = " ".join(fields.values()).casefold()
        if all(part in _HEADER for part in label.split()):
            continue
        rows.append(fields)
    return rows


def _int(value: str | None, width: int) -> int | None:
    if not value:
        return None
    match = _YEAR.match(value) if width == 4 else _NUM.match(value)
    if match is None:
        return None
    number = int(match.group(1))
    if width == 4 and not 1700 <= number <= 2026:
        return None
    if width < 4 and number > 31:
        return None
    return number


def _date(day: str | None, month: str | None, year: str | None) -> tuple[str, int | None]:
    day_n = _int(day, 2)
    month_n = _int(month, 2)
    year_n = _int(year, 4)
    parts = [str(part) for part in (day_n, month_n, year_n) if part is not None]
    return ".".join(parts), year_n


def records_from_pdf(path: Path, source: str) -> list[dict]:
    from pdfminer.high_level import extract_pages

    records = []
    for page in extract_pages(str(path)):
        for fields in _rows(page):
            cemetery = re.sub(r"\s+", " ", fields.get("cemetery", "")).strip()
            surname = fields.get("surname", "").strip()
            given = fields.get("given", "").strip()
            patronymic = fields.get("patronymic", "").strip()
            if not cemetery or (not surname and not given):
                continue
            if cemetery.casefold() == "кладбище" and surname.casefold() == "фамилия":
                continue
            if surname.casefold() == "неч":
                surname = ""
            birth, birth_year = _date(fields.get("bday"), fields.get("bmonth"), fields.get("byear"))
            death, death_year = _date(fields.get("dday"), fields.get("dmonth"), fields.get("dyear"))
            records.append(
                {
                    "cemetery": cemetery,
                    "surname": surname,
                    "given": given,
                    "patronymic": patronymic,
                    "birth": birth,
                    "death": death,
                    "birth_year": birth_year,
                    "death_year": death_year,
                    "source": source,
                }
            )
    return records
