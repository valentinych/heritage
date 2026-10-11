from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import LINK_PROPOSED, Dataset, LinkRow, Person, PersonLink, Tree
from app.util import utcnow

AMBIGUOUS_LIMIT = 12

OUTCOME_LABEL = {
    "unmatched": "нет пары",
    "single": "один человек",
    "linked": "есть пары",
    "ambiguous": "слишком много однофамильцев",
    "pending": "ещё не искали",
}


def match_dataset(db: Session, dataset: Dataset) -> str:
    people = db.execute(
        select(Person, Tree).join(Tree, Tree.id == Person.tree_id).where(Tree.space_id == dataset.space_id)
    ).all()
    by_name: dict[str, list[Person]] = {}
    by_name_year: dict[tuple[str, int], list[Person]] = {}
    for person, _tree in people:
        if not person.name_key:
            continue
        by_name.setdefault(person.name_key, []).append(person)
        if person.birth_year is not None:
            by_name_year.setdefault((person.name_key, person.birth_year), []).append(person)

    existing = {
        (link.person_low_id, link.person_high_id)
        for link in db.scalars(select(PersonLink).where(PersonLink.space_id == dataset.space_id))
    }
    rows = list(db.scalars(select(LinkRow).where(LinkRow.dataset_id == dataset.id)))
    created = 0
    unpaired = 0
    ambiguous = 0
    for row in rows:
        if row.birth_year is not None:
            found = by_name_year.get((row.name_key, row.birth_year), [])
        else:
            found = by_name.get(row.name_key, [])
        unique: list[Person] = []
        seen: set[int] = set()
        for person in found:
            if person.id not in seen:
                seen.add(person.id)
                unique.append(person)
        row.match_count = len(unique)
        trees = {person.tree_id for person in unique}
        if not row.name_key or not unique:
            row.outcome = "unmatched"
            unpaired += 1
            continue
        if len(unique) > AMBIGUOUS_LIMIT:
            row.outcome = "ambiguous"
            ambiguous += 1
            continue
        if len(trees) < 2:
            row.outcome = "single"
            unpaired += 1
            continue
        row.outcome = "linked"
        for index, left in enumerate(unique):
            for right in unique[index + 1 :]:
                if left.tree_id == right.tree_id:
                    continue
                low, high = sorted((left.id, right.id))
                if (low, high) in existing:
                    continue
                db.add(
                    PersonLink(
                        space_id=dataset.space_id,
                        dataset_id=dataset.id,
                        person_low_id=low,
                        person_high_id=high,
                        status=LINK_PROPOSED,
                        created_at=utcnow(),
                    )
                )
                existing.add((low, high))
                created += 1
    return f"Новые пары — {created}, без пары — {unpaired}, неоднозначно — {ambiguous}."


def dataset_link_report(db: Session, dataset: Dataset) -> dict[str, object]:
    rows = list(db.scalars(select(LinkRow).where(LinkRow.dataset_id == dataset.id)))
    counts = {"unmatched": 0, "single": 0, "linked": 0, "ambiguous": 0, "pending": 0}
    examples: list[str] = []
    hidden_pool = 0
    for row in rows:
        counts[row.outcome] = counts.get(row.outcome, 0) + 1
        if row.outcome in {"unmatched", "single", "ambiguous"}:
            hidden_pool += 1
            if len(examples) < 15:
                year = f", {row.birth_year}" if row.birth_year else ""
                label = OUTCOME_LABEL.get(row.outcome, row.outcome)
                examples.append(f"{row.name}{year} — {label}")
    summary = (
        f"Строк: {len(rows)}. С парами — {counts['linked']}, "
        f"без пары — {counts['unmatched'] + counts['single']}, "
        f"неоднозначно — {counts['ambiguous']}."
    )
    return {"summary": summary, "examples": examples, "hidden": max(0, hidden_pool - len(examples))}
