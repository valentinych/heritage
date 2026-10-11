from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.importers.names import parse_personal_name

_LINE = re.compile(r"^(\d+)\s+(.*)$")
_ANSEL = re.compile(r"(?m)^1 CHAR ANSEL\b")


@dataclass
class Record:
    xref: str | None
    tag: str
    value: str
    children: list[Record] = field(default_factory=list)


@dataclass
class Indi:
    xref: str
    given: str
    surname: str
    full: str
    sex: str
    birth: str
    death: str


@dataclass
class Fam:
    xref: str
    husband: str | None
    wife: str | None
    children: list[str]


def decode_gedcom(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1251")


def parse_line(line: str) -> tuple[int, str | None, str, str] | None:
    match = _LINE.match(line.rstrip("\r\n"))
    if match is None:
        return None
    level = int(match.group(1))
    rest = match.group(2)
    xref = None
    if rest.startswith("@"):
        xref, _, rest = rest.partition(" ")
    tag, separator, value = rest.partition(" ")
    if not separator:
        value = ""
    if not tag:
        return None
    return level, xref, tag, value


def parse_gedcom(text: str) -> list[Record]:
    roots: list[Record] = []
    stack: list[tuple[int, Record]] = []
    for raw in text.lstrip("\ufeff").splitlines():
        parsed = parse_line(raw)
        if parsed is None:
            continue
        level, xref, tag, value = parsed
        if tag in {"CONC", "CONT"} and stack:
            parent = stack[-1][1]
            if tag == "CONC":
                parent.value = f"{parent.value}{value}"
            else:
                parent.value = f"{parent.value}\n{value}"
            continue
        record = Record(xref=xref, tag=tag, value=value)
        while stack and stack[-1][0] >= level:
            stack.pop()
        if not stack:
            if level == 0:
                roots.append(record)
        else:
            stack[-1][1].children.append(record)
        stack.append((level, record))
    return roots


def _first(record: Record, tag: str) -> Record | None:
    for child in record.children:
        if child.tag == tag:
            return child
    return None


def _event_date(record: Record, tag: str) -> str:
    event = _first(record, tag)
    if event is None:
        return ""
    date = _first(event, "DATE")
    return date.value.strip() if date else ""


def _child_text(record: Record, tag: str) -> str:
    child = _first(record, tag)
    return child.value.strip() if child else ""


def extract(records: list[Record]) -> tuple[list[Indi], list[Fam]]:
    people: list[Indi] = []
    families: list[Fam] = []
    for record in records:
        if record.tag == "INDI" and record.xref:
            name_value = _child_text(record, "NAME")
            given, surname, full = parse_personal_name(name_value)
            givn = _child_text(record, "GIVN")
            surn = _child_text(record, "SURN")
            if not given and givn:
                given = givn
            if not surname and surn:
                surname = surn
            if not full:
                full = " ".join(part for part in (given, surname) if part)
            sex = _child_text(record, "SEX").upper()[:1]
            if sex not in {"M", "F"}:
                sex = ""
            people.append(
                Indi(
                    xref=record.xref,
                    given=given,
                    surname=surname,
                    full=full,
                    sex=sex,
                    birth=_event_date(record, "BIRT"),
                    death=_event_date(record, "DEAT"),
                )
            )
        elif record.tag == "FAM" and record.xref:
            children = [child.value.strip() for child in record.children if child.tag == "CHIL" and child.value.strip()]
            husband = _child_text(record, "HUSB") or None
            wife = _child_text(record, "WIFE") or None
            families.append(Fam(xref=record.xref, husband=husband, wife=wife, children=children))
    return people, families


def read_gedcom(data: bytes) -> tuple[list[Indi], list[Fam]]:
    text = decode_gedcom(data)
    if _ANSEL.search(text[:800]):
        raise ValueError("Кодировка ANSEL не поддерживается. Экспортируйте GEDCOM в UTF-8.")
    return extract(parse_gedcom(text))
