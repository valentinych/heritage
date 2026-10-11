from __future__ import annotations

import re

_PERSONAL_NAME = re.compile(r"^(?P<given>.*?)\s*/(?P<surname>[^/]*)/(?P<rest>.*)$")
_YEAR = re.compile(r"\b(\d{4})\b")
_TOKEN = re.compile(r"[^\w\s]", flags=re.UNICODE)


def parse_personal_name(value: str) -> tuple[str, str, str]:
    value = " ".join(value.replace("\n", " ").split())
    match = _PERSONAL_NAME.match(value)
    if match is None:
        return "", "", value.strip()
    given = " ".join(match.group("given").split())
    surname = " ".join(match.group("surname").split())
    rest = " ".join(match.group("rest").split())
    full = " ".join(part for part in (given, surname, rest) if part)
    return given, surname, full


def normalize_name(value: str) -> str:
    text = value.replace("ё", "е").replace("Ё", "Е").casefold()
    text = _TOKEN.sub(" ", text)
    tokens = [token for token in text.split() if token]
    return " ".join(sorted(tokens))


def extract_year(value: str | None) -> int | None:
    if not value:
        return None
    match = _YEAR.search(value)
    if match is None:
        return None
    year = int(match.group(1))
    if 1 <= year <= 2100:
        return year
    return None
