from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import LINK_CONFIRMED, Family, Person, PersonLink, Tree, family_child

SEX_LABEL = {"M": "мужчина", "F": "женщина"}


def person_card(db: Session, person: Person, tree: Tree) -> dict[str, object]:
    child_family_ids = list(
        db.scalars(select(family_child.c.family_id).where(family_child.c.person_id == person.id))
    )
    parent_groups = []
    if child_family_ids:
        families = list(db.scalars(select(Family).where(Family.id.in_(child_family_ids))))
        for family in families:
            parent_groups.append(
                {
                    "father": db.get(Person, family.husband_id) if family.husband_id else None,
                    "mother": db.get(Person, family.wife_id) if family.wife_id else None,
                }
            )

    spouse_families = list(
        db.scalars(
            select(Family).where(or_(Family.husband_id == person.id, Family.wife_id == person.id))
        )
    )
    spouse_groups = []
    for family in spouse_families:
        spouse_id = family.wife_id if family.husband_id == person.id else family.husband_id
        children = list(
            db.scalars(
                select(Person)
                .join(family_child, family_child.c.person_id == Person.id)
                .where(family_child.c.family_id == family.id)
                .order_by(family_child.c.position, Person.id)
            )
        )
        spouse_groups.append(
            {
                "spouse": db.get(Person, spouse_id) if spouse_id else None,
                "children": children,
            }
        )

    link_rows = list(
        db.scalars(
            select(PersonLink).where(
                PersonLink.status == LINK_CONFIRMED,
                or_(PersonLink.person_low_id == person.id, PersonLink.person_high_id == person.id),
            )
        )
    )
    confirmed = []
    for link in link_rows:
        other_id = link.person_high_id if link.person_low_id == person.id else link.person_low_id
        other = db.get(Person, other_id)
        if other is None:
            continue
        other_tree = db.get(Tree, other.tree_id)
        if other_tree is None or other_tree.space_id != tree.space_id:
            continue
        confirmed.append({"person": other, "tree": other_tree})

    return {
        "sex_label": SEX_LABEL.get(person.sex, "не указан"),
        "parent_groups": parent_groups,
        "spouse_groups": spouse_groups,
        "confirmed_links": confirmed,
    }
