"""Read a Novogrudok burial spreadsheet exported as CSV."""

from __future__ import annotations

import csv
import re
from pathlib import Path

# nid is a real token on the indexed sheets (unknown surname), so it stays searchable.
_BLANK_SURNAMES = {"unk", "неч"}
_PHOTO_NAME = re.compile(r"(?i)\.(?:jpe?g|png|gif|webp|tiff?|heic|bmp)$")
_NOTE_COLUMNS = (
    "Комментарий1",
    "Комментарий2",
    "Комментарий77",
    "Родство1",
    "Родство2",
    "Родство3",
)


def _cell(row: dict, name: str) -> str:
    return (row.get(name) or "").strip()


def _first(row: dict, *names: str) -> str:
    folded = {key.strip().casefold(): key for key in row}
    for name in names:
        key = folded.get(name.casefold())
        if key is None:
            continue
        value = _cell(row, key)
        if value:
            return value
    return ""


def _surname(value: str) -> str:
    if value.casefold() in _BLANK_SURNAMES:
        return ""
    return value


def _piece(value: str, year: bool) -> int | None:
    if not re.fullmatch(r"\d{1,4}", value):
        return None
    number = int(value)
    if year:
        return number if 1700 <= number <= 2026 else None
    return number if number <= 31 else None


def _date(day: str, month: str, year: str) -> tuple[str, int | None]:
    day_n = _piece(day, False)
    month_n = _piece(month, False)
    year_n = _piece(year, True)
    parts = [str(part) for part in (day_n, month_n, year_n) if part is not None]
    return ".".join(parts), year_n


def _note(row: dict) -> str:
    parts = []
    seen = set()
    for column in _NOTE_COLUMNS:
        text = _cell(row, column)
        if not text or text.casefold() in seen:
            continue
        seen.add(text.casefold())
        parts.append(text)
    return ". ".join(parts)


def _spelling(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def _original_spelling(row: dict, surname: str, given: str, patronymic: str) -> str:
    """Keep a Polish or other original name when it is not the Russian form used for search."""
    original = " ".join(
        part for part in (_cell(row, "Фамилия"), _cell(row, "Имя"), _cell(row, "Отчество")) if part
    )
    used = " ".join(part for part in (surname, given, patronymic) if part)
    if not original or _spelling(original) == _spelling(used):
        return ""
    return original


def _photo_only(*values: str) -> bool:
    filled = [value for value in values if value]
    return bool(filled) and all(_PHOTO_NAME.search(value) for value in filled)


def _url(value: str) -> str:
    if value.startswith("https://") or value.startswith("http://"):
        return value
    return ""


def records_from_csv(path: Path, source: str, cemetery: str = "") -> list[dict]:
    records = []
    fallback = re.sub(r"\s+", " ", cemetery).strip()
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            cemetery_name = re.sub(r"\s+", " ", _cell(row, "Кладбище")) or fallback
            surname = _surname(_first(row, "Фамилия RU", "Фамилия"))
            given = _first(row, "Имя RU", "Имя")
            patronymic = _first(row, "Отчество RU", "Отчество")
            if not cemetery_name or cemetery_name.casefold() == "кладбище":
                continue
            birth, birth_year = _date(_cell(row, "РоД"), _cell(row, "РоМ"), _cell(row, "РоГ"))
            death, death_year = _date(_cell(row, "СмД"), _cell(row, "СмМ"), _cell(row, "СмГ"))
            sector = _cell(row, "Сектор")
            grave_row = _cell(row, "Ряд")
            plot = _cell(row, "Место")
            note = _note(row)
            if _photo_only(surname, given, patronymic, note) and not any(
                (birth, death, sector, grave_row, plot)
            ):
                continue
            original = _original_spelling(row, surname, given, patronymic)
            if original and original.casefold() not in note.casefold():
                note = (note + ". " + original).strip(". ") if note else original
            if not any((surname, given, patronymic, birth, death, sector, grave_row, plot, note)):
                continue
            records.append(
                {
                    "cemetery": cemetery_name,
                    "surname": surname,
                    "given": given,
                    "patronymic": patronymic,
                    "birth": birth,
                    "death": death,
                    "birth_year": birth_year,
                    "death_year": death_year,
                    "sector": sector,
                    "row": grave_row,
                    "plot": plot,
                    "note": note,
                    "url": _url(_cell(row, "На карте Google maps")),
                    "source": source,
                }
            )
    return records
