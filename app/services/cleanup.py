from __future__ import annotations

from pathlib import Path

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from app.models import (
    ClusterMember,
    ClusterPair,
    Dataset,
    DnaMatch,
    Family,
    LinkBuild,
    LinkRow,
    Membership,
    Person,
    PersonLink,
    Space,
    Tree,
    User,
    family_child,
)
from app.services.storage import remove_file


def delete_tree_rows(db: Session, tree: Tree) -> None:
    person_ids = select(Person.id).where(Person.tree_id == tree.id)
    db.execute(
        delete(PersonLink).where(
            or_(
                PersonLink.person_low_id.in_(person_ids),
                PersonLink.person_high_id.in_(person_ids),
            )
        )
    )
    family_ids = select(Family.id).where(Family.tree_id == tree.id)
    db.execute(delete(family_child).where(family_child.c.family_id.in_(family_ids)))
    db.execute(delete(Family).where(Family.tree_id == tree.id))
    db.execute(delete(Person).where(Person.tree_id == tree.id))
    db.execute(delete(Tree).where(Tree.id == tree.id))


def delete_dataset_rows(db: Session, dataset: Dataset) -> None:
    db.execute(
        delete(PersonLink).where(
            PersonLink.dataset_id == dataset.id,
            PersonLink.status == "proposed",
        )
    )
    db.execute(update(PersonLink).where(PersonLink.dataset_id == dataset.id).values(dataset_id=None))
    db.execute(delete(DnaMatch).where(DnaMatch.dataset_id == dataset.id))
    db.execute(delete(ClusterPair).where(ClusterPair.dataset_id == dataset.id))
    db.execute(delete(ClusterMember).where(ClusterMember.dataset_id == dataset.id))
    db.execute(delete(LinkRow).where(LinkRow.dataset_id == dataset.id))
    db.execute(delete(Dataset).where(Dataset.id == dataset.id))


def remove_tree(db: Session, data_dir: Path, tree: Tree) -> None:
    path = tree.stored_path
    delete_tree_rows(db, tree)
    db.commit()
    remove_file(data_dir, path)


def remove_dataset(db: Session, data_dir: Path, dataset: Dataset) -> None:
    path = dataset.stored_path
    delete_dataset_rows(db, dataset)
    db.commit()
    remove_file(data_dir, path)


def remove_space(db: Session, data_dir: Path, space: Space) -> None:
    trees = list(db.scalars(select(Tree).where(Tree.space_id == space.id)))
    datasets = list(db.scalars(select(Dataset).where(Dataset.space_id == space.id)))
    paths = [item.stored_path for item in trees] + [item.stored_path for item in datasets]
    db.execute(delete(PersonLink).where(PersonLink.space_id == space.id))
    db.execute(delete(LinkBuild).where(LinkBuild.space_id == space.id))
    for tree in trees:
        delete_tree_rows(db, tree)
    for dataset in datasets:
        delete_dataset_rows(db, dataset)
    db.execute(delete(Membership).where(Membership.space_id == space.id))
    db.execute(delete(Space).where(Space.id == space.id))
    db.commit()
    for path in paths:
        remove_file(data_dir, path)


def remove_user(db: Session, actor: User, account: User) -> None:
    if account.id == actor.id:
        raise ValueError("Нельзя удалить свою учётную запись.")
    if account.is_admin:
        others = db.scalar(select(User).where(User.is_admin.is_(True), User.id != account.id))
        if others is None:
            raise ValueError("Нельзя удалить единственного администратора.")
    db.execute(delete(User).where(User.id == account.id))
    db.commit()
