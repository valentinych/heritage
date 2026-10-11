from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.importers.names import extract_year, normalize_name
from app.importers.tables import read_table
from app.models import KIND_LINKS, Dataset, LinkBuild, LinkRow, Person, Tree
from app.services.storage import resolve_path

MIN_MARRIAGE_AGE = 16
MAX_MARRIAGE_AGE = 35
MIN_PARENT_AGE = 16
MAX_PARENT_AGE = 60
MAX_LIFESPAN = 100
PEOPLE_PAGE = 50
_KIND = {"рождение": "birth", "брак": "marriage", "смерть": "death"}


@dataclass
class Record:
    dataset_id: int
    slot: int
    kind: str
    year: int | None
    given: str
    surname: str
    parish: str
    spouse_given: str = ""
    spouse_surname: str = ""
    parish_label: str = ""
    act: str = ""
    father: str = ""
    mother: str = ""
    mother_surname: str = ""
    parents: str = ""
    spouse_parents: str = ""


@dataclass
class Identity:
    given: str
    surname: str
    parish: str
    birth_year: int | None
    birth: Record
    marriages: list[Record] = field(default_factory=list)
    death: Record | None = None
    ambiguous: bool = False
    father_match: str = ""
    mother_match: str = ""


@dataclass
class ClusterResult:
    identities: list[Identity]
    ambiguous: int
    cross: int


def parish_key(value: str) -> str:
    text = value.replace("ё", "е").replace("Ё", "Е").casefold()
    return " ".join(text.split())


def _name_key(given: str, surname: str) -> tuple[str, str]:
    return normalize_name(given), normalize_name(surname)


def _mark(items: list[Identity]) -> None:
    for item in items:
        item.ambiguous = True


def _choose(pool: list[Identity], parish: str) -> tuple[Identity | None, bool, bool]:
    same = [item for item in pool if parish and item.parish == parish]
    if len(same) > 1:
        _mark(same)
        return None, False, True
    if len(same) == 1:
        return same[0], False, False
    if len(pool) > 1:
        _mark(pool)
        return None, False, True
    if len(pool) == 1:
        ident = pool[0]
        cross = bool(parish and ident.parish and ident.parish != parish)
        return ident, cross, False
    return None, False, False


def _marriage_ok(ident: Identity, year: int | None) -> bool:
    if year is None:
        return False
    age = year - ident.birth_year
    return MIN_MARRIAGE_AGE <= age <= MAX_MARRIAGE_AGE


def _death_ok(ident: Identity, year: int | None) -> bool:
    if year is None or year <= ident.birth_year or year - ident.birth_year > MAX_LIFESPAN:
        return False
    married = [item.year for item in ident.marriages if item.year is not None]
    if married and year <= max(married):
        return False
    return True


def _sides(record: Record) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    pairs = [(record.given, record.surname)]
    if record.kind == "marriage":
        pairs.append((record.spouse_given, record.spouse_surname))
    for given, surname in pairs:
        key = _name_key(given, surname)
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        found.append((given, surname))
    return found


def cluster_records(records: list[Record]) -> ClusterResult:
    by_name: dict[tuple[str, str], list[Identity]] = {}
    identities: list[Identity] = []
    for record in records:
        if record.kind != "birth":
            continue
        key = _name_key(record.given, record.surname)
        if not key[0] or not key[1]:
            continue
        ident = Identity(
            given=record.given,
            surname=record.surname,
            parish=record.parish,
            birth_year=record.year,
            birth=record,
        )
        identities.append(ident)
        by_name.setdefault(key, []).append(ident)

    ambiguous = 0
    cross = 0
    for record in records:
        if record.kind != "marriage":
            continue
        for given, surname in _sides(record):
            pool = [
                item
                for item in by_name.get(_name_key(given, surname), [])
                if item.birth_year is not None and _marriage_ok(item, record.year)
            ]
            chosen, crossed, unclear = _choose(pool, record.parish)
            if unclear:
                ambiguous += 1
            elif chosen is not None:
                chosen.marriages.append(record)
                cross += int(crossed)

    wanted: list[tuple[Identity, Record, bool]] = []
    for record in records:
        if record.kind != "death":
            continue
        for given, surname in _sides(record):
            pool = [
                item
                for item in by_name.get(_name_key(given, surname), [])
                if item.birth_year is not None and _death_ok(item, record.year)
            ]
            chosen, crossed, unclear = _choose(pool, record.parish)
            if unclear:
                ambiguous += 1
            elif chosen is not None:
                wanted.append((chosen, record, crossed))

    grouped: dict[int, list[tuple[Identity, Record, bool]]] = {}
    for item in wanted:
        grouped.setdefault(id(item[0]), []).append(item)
    for group in grouped.values():
        if len(group) != 1:
            ambiguous += len(group)
            _mark([ident for ident, _record, _crossed in group])
            continue
        ident, record, crossed = group[0]
        ident.death = record
        cross += int(crossed)
    return ClusterResult(identities=identities, ambiguous=ambiguous, cross=cross)


def _parse_record(dataset_id: int, slot: int, row: dict[str, str]) -> Record | None:
    kind = _KIND.get(row.get("Событие", "").strip().casefold())
    if kind is None:
        return None
    year = extract_year(row.get("Год") or "")
    if kind == "birth" and year is None:
        year = extract_year(row.get("Год рождения") or "")
    raw_parish = " ".join((row.get("Приход") or "").split())
    return Record(
        dataset_id=dataset_id,
        slot=slot,
        kind=kind,
        year=year,
        given=row.get("Имя", "").strip(),
        surname=row.get("Фамилия", "").strip(),
        parish=parish_key(raw_parish),
        spouse_given=row.get("Имя супруга", "").strip(),
        spouse_surname=row.get("Фамилия супруга", "").strip(),
        parish_label=raw_parish,
        act=row.get("Акт", "").strip(),
        father=row.get("Имя отца", "").strip(),
        mother=row.get("Имя матери", "").strip(),
        mother_surname=row.get("Фамилия матери", "").strip(),
        parents=row.get("Родители", "").strip(),
        spouse_parents=row.get("Родители супруга", "").strip(),
    )


def _tree_matches(ident: Identity, people: dict[tuple[str, str], list[Person]]) -> list[Person]:
    found: list[Person] = []
    seen: set[int] = set()
    for person in people.get(_name_key(ident.given, ident.surname), []):
        if person.id in seen:
            continue
        if person.birth_year is not None and person.birth_year != ident.birth_year:
            continue
        seen.add(person.id)
        found.append(person)
    return found


def _source(ident: Identity) -> Record:
    if ident.birth is not None:
        return ident.birth
    if ident.marriages:
        return ident.marriages[0]
    return ident.death


def _fold(value: str) -> str:
    text = value.replace("ё", "е").replace("Ё", "Е").casefold()
    return " ".join(text.split())


def _parish_label(record: Record) -> str:
    return record.parish_label or record.parish


def _other_parishes(ident: Identity) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    events = list(ident.marriages)
    if ident.death is not None:
        events.append(ident.death)
    for event in events:
        if not event.parish or event.parish == ident.parish or event.parish in seen:
            continue
        seen.add(event.parish)
        labels.append(_parish_label(event))
    return labels


def _filled(*values: str) -> bool:
    return any(value.strip() for value in values)


def split_parents(text: str) -> tuple[str, str, str, str] | None:
    """Split «Imię, Imię Nazwisko» or «Imię Nazwisko, Imię Nazwisko».

    Returns father given name, father surname, mother given name, mother surname.
    Father surname is empty when the string does not give him one.
    """
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    left = parts[0].split()
    right = parts[1].split()
    if len(right) != 2:
        return None
    if len(left) == 1:
        return left[0], "", right[0], right[1]
    if len(left) == 2:
        return left[0], left[1], right[0], right[1]
    return None


def _separate_parents(record: Record) -> tuple[str, str, str]:
    if _filled(record.father, record.mother, record.mother_surname):
        return record.father, record.mother, record.mother_surname
    return "", "", ""


def _apply_parent_string(text: str) -> tuple[str, str, str, str, str]:
    """father, father surname, mother, mother surname, unsplit remainder."""
    parsed = split_parents(text)
    if parsed is None:
        return "", "", "", "", text.strip()
    father, father_surname, mother, mother_surname = parsed
    return father, father_surname, mother, mother_surname, ""


def _marriage_parents(ident: Identity, record: Record) -> tuple[str, str]:
    person = _name_key(ident.given, ident.surname)
    if person == _name_key(record.spouse_given, record.spouse_surname):
        return record.spouse_parents, record.parents
    return record.parents, record.spouse_parents


def _person_parents(ident: Identity) -> dict[str, str]:
    father, mother, mother_surname = _separate_parents(ident.birth)
    father_surname = ""
    parents = ""
    if _filled(father, mother, mother_surname):
        pass
    elif ident.birth.parents.strip():
        father, father_surname, mother, mother_surname, parents = _apply_parent_string(ident.birth.parents)
    else:
        for marriage in ident.marriages:
            own, _spouse = _marriage_parents(ident, marriage)
            if not own.strip():
                continue
            father, father_surname, mother, mother_surname, parents = _apply_parent_string(own)
            break
        if not _filled(father, mother, mother_surname, parents) and ident.death is not None:
            father, mother, mother_surname = _separate_parents(ident.death)
            father_surname = ""
            parents = ""
            if not _filled(father, mother, mother_surname) and ident.death.parents.strip():
                father, father_surname, mother, mother_surname, parents = _apply_parent_string(ident.death.parents)
    spouse_parents = ""
    for marriage in ident.marriages:
        _own, spouse = _marriage_parents(ident, marriage)
        if not spouse.strip():
            continue
        spouse_father, spouse_father_surname, spouse_mother, spouse_mother_surname, spouse_raw = _apply_parent_string(spouse)
        if spouse_raw:
            spouse_parents = spouse_raw
        else:
            father_label = " ".join(part for part in (spouse_father, spouse_father_surname) if part)
            mother_label = " ".join(part for part in (spouse_mother, spouse_mother_surname) if part)
            spouse_parents = ", ".join(part for part in (father_label, mother_label) if part)
        break
    shown_father = " ".join(part for part in (father, father_surname) if part)
    return {
        "father": shown_father,
        "father_given": father,
        "father_surname": father_surname,
        "mother": mother,
        "mother_surname": mother_surname,
        "parents": parents,
        "spouse_parents": spouse_parents,
    }


def _parent_gap_ok(parent: Identity, child_year: int | None) -> bool:
    if child_year is None or parent.birth_year is None:
        return False
    gap = child_year - parent.birth_year
    return MIN_PARENT_AGE <= gap <= MAX_PARENT_AGE


def _match_label(ident: Identity) -> str:
    bits = [str(ident.birth_year)]
    if ident.birth.act:
        bits.append(f"акт {ident.birth.act}")
    label = _parish_label(ident.birth)
    if label:
        bits.append(label)
    return ", ".join(bits)


def _parent_match(by_name: dict[tuple[str, str], list[Identity]], child: Identity, given: str, surname: str) -> str:
    if not given.strip() or not surname.strip():
        return ""
    pool = [
        item
        for item in by_name.get(_name_key(given, surname), [])
        if item is not child and _parent_gap_ok(item, child.birth_year)
    ]
    chosen, _crossed, unclear = _choose(pool, child.parish)
    if unclear:
        return "неоднозначно"
    if chosen is None:
        return ""
    return _match_label(chosen)


def resolve_parents(identities: list[Identity]) -> None:
    by_name: dict[tuple[str, str], list[Identity]] = {}
    for ident in identities:
        by_name.setdefault(_name_key(ident.given, ident.surname), []).append(ident)
    for ident in identities:
        facts = _person_parents(ident)
        father_surname = facts["father_surname"] or (ident.surname if facts["father_given"] else "")
        ident.father_match = _parent_match(by_name, ident, facts["father_given"], father_surname)
        ident.mother_match = _parent_match(by_name, ident, facts["mother"], facts["mother_surname"])


def _payload(ident: Identity, person_id: int | None) -> dict:
    others = _other_parishes(ident)
    home = _parish_label(ident.birth)
    return {
        "surname": ident.surname,
        "given": ident.given,
        "birth_year": ident.birth_year,
        "death_year": ident.death.year if ident.death is not None else None,
        "marriages": [item.year for item in ident.marriages if item.year is not None],
        "parish": home if not others else f"{home} · {', '.join(others)}",
        "other_parishes": others,
        "ambiguous": ident.ambiguous,
        "person_id": person_id,
        "act": ident.birth.act,
        "parish_key": ident.parish,
        "father_match": ident.father_match,
        "mother_match": ident.mother_match,
        **{key: value for key, value in _person_parents(ident).items() if key not in {"father_given", "father_surname"}},
    }


def _save_build(db: Session, space_id: int, rows: list[dict]) -> None:
    rows.sort(key=lambda item: (_fold(item["surname"]), _fold(item["given"]), item["birth_year"] or 0))
    payload = json.dumps(rows, ensure_ascii=False)
    build = db.get(LinkBuild, space_id)
    if build is None:
        db.add(LinkBuild(space_id=space_id, people=payload))
    else:
        build.people = payload


def _ready_records(db: Session, data_dir, space_id: int) -> tuple[list[Record], dict[int, list[LinkRow]]]:
    datasets = list(
        db.scalars(
            select(Dataset).where(
                Dataset.space_id == space_id,
                Dataset.kind == KIND_LINKS,
                Dataset.status == "ready",
            )
        )
    )
    records: list[Record] = []
    rows_by_dataset: dict[int, list[LinkRow]] = {}
    for dataset in datasets:
        if not dataset.column_map:
            continue
        try:
            mapping = json.loads(dataset.column_map)
            path = resolve_path(data_dir, dataset.stored_path)
            _headers, table = read_table(path)
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            continue
        name_col = str(mapping.get("name") or "")
        link_rows = list(
            db.scalars(select(LinkRow).where(LinkRow.dataset_id == dataset.id).order_by(LinkRow.id))
        )
        rows_by_dataset[dataset.id] = link_rows
        slot = 0
        for row in table:
            if not str(row.get(name_col, "")).strip():
                continue
            parsed = _parse_record(dataset.id, slot, row)
            if parsed is not None:
                records.append(parsed)
            slot += 1
    return records, rows_by_dataset


def assemble_link_trees(db: Session, data_dir, space_id: int) -> str:
    records, rows_by_dataset = _ready_records(db, data_dir, space_id)

    clustered = cluster_records(records)
    people: dict[tuple[str, str], list[Person]] = {}
    for person in db.scalars(select(Person).join(Tree, Tree.id == Person.tree_id).where(Tree.space_id == space_id)):
        key = _name_key(person.given_name, person.surname)
        if key[0] and key[1]:
            people.setdefault(key, []).append(person)

    ambiguous = clustered.ambiguous
    pending: list[tuple[Identity, Person]] = []
    claimed: dict[int, int] = {}
    for ident in clustered.identities:
        matches = _tree_matches(ident, people)
        if len(matches) > 1:
            ambiguous += 1
            ident.ambiguous = True
            continue
        if len(matches) != 1:
            continue
        person = matches[0]
        pending.append((ident, person))
        claimed[person.id] = claimed.get(person.id, 0) + 1

    desired: dict[int, int] = {}
    linked: dict[int, int] = {}
    tied = 0
    for ident, person in pending:
        if claimed[person.id] != 1:
            ambiguous += 1
            ident.ambiguous = True
            continue
        source = _source(ident)
        rows = rows_by_dataset.get(source.dataset_id, [])
        if source.slot >= len(rows):
            continue
        desired[rows[source.slot].id] = person.id
        linked[id(ident)] = person.id
        tied += 1

    for rows in rows_by_dataset.values():
        for row in rows:
            row.person_id = desired.get(row.id)
    resolve_parents(clustered.identities)
    _save_build(
        db,
        space_id,
        [_payload(ident, linked.get(id(ident))) for ident in clustered.identities],
    )
    return (
        f"К дереву привязано: {tied}. Неоднозначно: {ambiguous}. "
        f"Между приходами: {clustered.cross}."
    )


def _birth_key(given: str, surname: str, year: int | None, parish: str, act: str) -> tuple:
    return (_fold(given), _fold(surname), year, parish, act)


def _plain_birth(record: Record) -> dict:
    return {
        "surname": record.surname,
        "given": record.given,
        "father": record.father,
        "mother": record.mother,
        "mother_surname": record.mother_surname,
        "father_match": "",
        "mother_match": "",
        "parents": record.parents,
        "spouse_parents": record.spouse_parents,
        "birth_year": record.year,
        "death_year": None,
        "marriage": "",
        "parish": record.parish_label or record.parish,
        "other": "",
        "person_id": None,
        "person_name": "",
    }


def assembled_people(db: Session, data_dir, space_id: int, query: str) -> list[dict] | None:
    build = db.get(LinkBuild, space_id)
    if build is None:
        return None
    try:
        rows = json.loads(build.people or "[]")
    except json.JSONDecodeError:
        rows = []
    if not isinstance(rows, list):
        rows = []
    rows = [row for row in rows if isinstance(row, dict)]
    ids = [row["person_id"] for row in rows if isinstance(row.get("person_id"), int)]
    found: dict[int, tuple[Person, Tree]] = {}
    if ids:
        pairs = db.execute(
            select(Person, Tree)
            .join(Tree, Tree.id == Person.tree_id)
            .where(Tree.space_id == space_id, Person.id.in_(ids))
        ).all()
        found = {person.id: (person, tree) for person, tree in pairs}
    view: list[dict] = []
    for row in rows:
        pair = found.get(row.get("person_id")) if isinstance(row.get("person_id"), int) else None
        person = pair[0] if pair else None
        tree = pair[1] if pair else None
        others = row.get("other_parishes") or []
        if row.get("ambiguous"):
            other = "неоднозначно"
        elif others:
            other = ", ".join(str(item) for item in others)
        elif tree is not None:
            other = tree.filename
        else:
            other = ""
        marriages = row.get("marriages") or []
        view.append(
            {
                "surname": row.get("surname") or "",
                "given": row.get("given") or "",
                "father": row.get("father") or "",
                "mother": row.get("mother") or "",
                "mother_surname": row.get("mother_surname") or "",
                "father_match": row.get("father_match") or "",
                "mother_match": row.get("mother_match") or "",
                "parents": row.get("parents") or "",
                "spouse_parents": row.get("spouse_parents") or "",
                "birth_year": row.get("birth_year"),
                "death_year": row.get("death_year"),
                "marriage": ", ".join(str(year) for year in marriages if year is not None),
                "parish": row.get("parish") or "",
                "other": other,
                "person_id": person.id if person else None,
                "person_name": person.full_name if person else "",
                "parish_key": str(row.get("parish_key") or ""),
                "act": str(row.get("act") or ""),
            }
        )
    grouped: dict[tuple, list[dict]] = {}
    for item in view:
        key = _birth_key(
            str(item.get("given") or ""),
            str(item.get("surname") or ""),
            item.get("birth_year") if isinstance(item.get("birth_year"), int) else None,
            str(item.get("parish_key") or ""),
            str(item.get("act") or ""),
        )
        grouped.setdefault(key, []).append(item)
    stored = [record for record in _ready_records(db, data_dir, space_id)[0] if record.kind == "birth"]
    if stored:
        listed: list[dict] = []
        for record in stored:
            matches = grouped.get(_birth_key(record.given, record.surname, record.year, record.parish, record.act))
            if matches is not None and len(matches) == 1:
                listed.append(matches[0])
            else:
                listed.append(_plain_birth(record))
        view = listed
    needle = _fold(query)
    if needle:
        view = [item for item in view if needle in _fold(str(item.get("surname", "")))]
    return view
