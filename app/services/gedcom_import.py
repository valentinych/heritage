from __future__ import annotations

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.importers.gedcom import read_gedcom
from app.importers.names import extract_year, normalize_name
from app.models import Family, Person, Tree, family_child
from app.util import utcnow


def import_gedcom(
    db: Session,
    *,
    space_id: int,
    user_id: int,
    filename: str,
    stored_path: str,
    content: bytes,
) -> Tree:
    people, families = read_gedcom(content)
    if not people:
        raise ValueError("В файле нет людей.")
    tree = Tree(
        space_id=space_id,
        filename=filename,
        stored_path=stored_path,
        person_count=0,
        uploaded_by=user_id,
        created_at=utcnow(),
    )
    db.add(tree)
    db.flush()

    xref_to_id: dict[str, int] = {}
    seen_xref: set[str] = set()
    pending = 0
    for indi in people:
        if indi.xref in seen_xref:
            continue
        seen_xref.add(indi.xref)
        full = indi.full.strip() or "Без имени"
        person = Person(
            tree_id=tree.id,
            gedcom_id=indi.xref,
            given_name=indi.given[:255],
            surname=indi.surname[:255],
            full_name=full[:500],
            name_key=normalize_name(full) if indi.full.strip() else "",
            sex=indi.sex,
            birth_date=indi.birth[:80],
            birth_year=extract_year(indi.birth),
            death_date=indi.death[:80],
            death_year=extract_year(indi.death),
        )
        db.add(person)
        pending += 1
        if pending >= 400:
            db.flush()
            pending = 0
    db.flush()
    for person in db.scalars(select(Person).where(Person.tree_id == tree.id)):
        xref_to_id[person.gedcom_id] = person.id

    seen_families: set[str] = set()
    batch: list[tuple[Family, list[int]]] = []
    for fam in families:
        if fam.xref in seen_families:
            continue
        seen_families.add(fam.xref)
        child_ids: list[int] = []
        seen_children: set[int] = set()
        for child in fam.children:
            child_id = xref_to_id.get(child)
            if child_id and child_id not in seen_children:
                seen_children.add(child_id)
                child_ids.append(child_id)
        husband_id = xref_to_id.get(fam.husband) if fam.husband else None
        wife_id = xref_to_id.get(fam.wife) if fam.wife else None
        if husband_id is None and wife_id is None and not child_ids:
            continue
        family = Family(
            tree_id=tree.id,
            gedcom_id=fam.xref,
            husband_id=husband_id,
            wife_id=wife_id,
        )
        db.add(family)
        batch.append((family, child_ids))
        if len(batch) >= 200:
            _flush_families(db, batch)
            batch = []
    if batch:
        _flush_families(db, batch)

    tree.person_count = len(xref_to_id)
    return tree


def _flush_families(db: Session, batch: list[tuple[Family, list[int]]]) -> None:
    db.flush()
    rows = []
    for family, child_ids in batch:
        for position, child_id in enumerate(child_ids):
            rows.append({"family_id": family.id, "person_id": child_id, "position": position})
    if rows:
        db.execute(insert(family_child), rows)
