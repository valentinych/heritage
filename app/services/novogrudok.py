from __future__ import annotations

import csv
import io
import re
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GedMatchHit, GedMatchKit

SHEET_CSV = (
    "https://docs.google.com/spreadsheets/d/11AkC4lBttNndyMoM835ZzARDjJvKU16A4Tb4mURGL2Q/export?format=csv&gid=0"
)
SNAPSHOT = Path(__file__).resolve().parent.parent / "novogrudok_kits.txt"
SPACE_ID = 1
_KIT = re.compile(r"^[A-Z]{1,3}\d{4,10}$")


def parse_kit_column(text: str) -> list[str]:
    kits: list[str] = []
    seen: set[str] = set()
    for row in csv.reader(io.StringIO(text.lstrip("\ufeff"))):
        if not row:
            continue
        cell = row[0].strip().upper()
        if "@" in cell or cell in seen or _KIT.fullmatch(cell) is None:
            continue
        seen.add(cell)
        kits.append(cell)
    return kits


def load_column_kits() -> list[str]:
    try:
        with httpx.Client(timeout=4.0, follow_redirects=True) as client:
            response = client.get(SHEET_CSV)
            response.raise_for_status()
        kits = parse_kit_column(response.text)
        if kits:
            return kits
    except httpx.HTTPError:
        pass
    if not SNAPSHOT.is_file():
        return []
    return parse_kit_column(SNAPSHOT.read_text(encoding="utf-8"))


def saved_page_count(db: Session, space_id: int, kits: list[str]) -> int:
    if not kits:
        return 0
    found = set(
        db.scalars(
            select(GedMatchHit.source_kit)
            .where(GedMatchHit.space_id == space_id, GedMatchHit.source_kit.in_(kits))
            .distinct()
        )
    )
    found.update(
        db.scalars(
            select(GedMatchKit.source_kit).where(
                GedMatchKit.space_id == space_id,
                GedMatchKit.source_kit.in_(kits),
            )
        )
    )
    return len(found)
