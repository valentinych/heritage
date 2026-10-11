from __future__ import annotations

import csv
import io

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.importers.geneteka import COLUMNS, GenetekaLoad
from app.importers.tables import read_table
from app.models import KIND_LINKS, Dataset, Space, User
from app.services.matching import match_dataset
from app.services.storage import remove_file, resolve_path, save_bytes
from app.services.table_import import replace_imported
from app.util import utcnow

_MAP = {"name": "Фио", "birth_year": "Год рождения"}


def to_csv(rows: list[dict[str, str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(COLUMNS), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in COLUMNS})
    return buffer.getvalue().encode("utf-8-sig")


def store_geneteka(db: Session, data_dir, space: Space, user: User, loaded: GenetekaLoad) -> tuple[str, str | None]:
    if not loaded.rows:
        raise ValueError(loaded.message() or "В индексе нет строк.")
    content = to_csv(loaded.rows)
    relative = save_bytes(data_dir, space.id, loaded.filename, content)
    existing = db.scalar(
        select(Dataset).where(
            Dataset.space_id == space.id,
            Dataset.kind == KIND_LINKS,
            Dataset.filename == loaded.filename,
        )
    )
    old = None
    if existing is None:
        dataset = Dataset(
            space_id=space.id,
            kind=KIND_LINKS,
            filename=loaded.filename,
            stored_path=relative,
            status="mapping",
            column_map=None,
            row_count=0,
            uploaded_by=user.id,
            created_at=utcnow(),
        )
        db.add(dataset)
    else:
        old = existing.stored_path
        existing.stored_path = relative
        existing.uploaded_by = user.id
        existing.created_at = utcnow()
        dataset = existing
    db.flush()
    try:
        path = resolve_path(data_dir, relative)
        _headers, records = read_table(path)
        replace_imported(db, dataset, _MAP, records)
        match_dataset(db, dataset)
    except Exception:
        remove_file(data_dir, relative)
        raise
    return loaded.message(), old
