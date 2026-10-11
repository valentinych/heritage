from __future__ import annotations

import json

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.importers.names import extract_year, normalize_name
from app.importers.tables import mapped_rows, parse_float
from app.models import ClusterMember, ClusterPair, Dataset, DnaMatch, LinkRow, PersonLink


def replace_imported(db: Session, dataset: Dataset, mapping: dict[str, str], rows: list[dict[str, str]]) -> int:
    db.execute(delete(DnaMatch).where(DnaMatch.dataset_id == dataset.id))
    db.execute(delete(ClusterPair).where(ClusterPair.dataset_id == dataset.id))
    db.execute(delete(ClusterMember).where(ClusterMember.dataset_id == dataset.id))
    db.execute(
        delete(PersonLink).where(
            PersonLink.dataset_id == dataset.id,
            PersonLink.status == "proposed",
        )
    )
    db.execute(delete(LinkRow).where(LinkRow.dataset_id == dataset.id))
    projected = mapped_rows(rows, mapping)
    if dataset.kind == "matches":
        count = _matches(db, dataset, projected)
    elif dataset.kind == "clusters":
        count = _clusters(db, dataset, projected)
    else:
        count = _links(db, dataset, projected)
    if count == 0:
        raise ValueError("В выбранных столбцах нет имён.")
    dataset.column_map = json.dumps(mapping, ensure_ascii=False)
    dataset.status = "ready"
    dataset.row_count = count
    return count


def _matches(db: Session, dataset: Dataset, rows: list[dict[str, str]]) -> int:
    count = 0
    for row in rows:
        name = row.get("name", "").strip()
        if not name:
            continue
        db.add(
            DnaMatch(
                dataset_id=dataset.id,
                space_id=dataset.space_id,
                name=name[:500],
                shared_cm=parse_float(row.get("shared_cm", "")),
                largest_segment=parse_float(row.get("largest_segment", "")),
            )
        )
        count += 1
    return count


def _clusters(db: Session, dataset: Dataset, rows: list[dict[str, str]]) -> int:
    count = 0
    for row in rows:
        name = row.get("name", "").strip()
        label = row.get("cluster", "").strip()
        if not name or not label:
            continue
        db.add(
            ClusterMember(
                dataset_id=dataset.id,
                space_id=dataset.space_id,
                cluster_label=label[:80],
                name=name[:500],
                shared_cm=parse_float(row.get("shared_cm", "")),
            )
        )
        count += 1
    return count


def _links(db: Session, dataset: Dataset, rows: list[dict[str, str]]) -> int:
    count = 0
    for row in rows:
        name = row.get("name", "").strip()
        if not name:
            continue
        db.add(
            LinkRow(
                dataset_id=dataset.id,
                space_id=dataset.space_id,
                name=name[:500],
                name_key=normalize_name(name),
                birth_year=extract_year(row.get("birth_year", "")),
                outcome="pending",
                match_count=0,
            )
        )
        count += 1
    return count
