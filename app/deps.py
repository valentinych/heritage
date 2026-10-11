from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Dataset, Membership, Person, PersonLink, Space, Tree, User


class NotAuthenticated(Exception):
    pass


def get_db(request: Request):
    db = request.app.state.session_factory()
    try:
        yield db
    finally:
        db.close()


def load_session(request: Request, db: Session) -> tuple[User | None, list[Space]]:
    user_id = request.session.get("user_id")
    if not user_id:
        return None, []
    user = db.get(User, user_id)
    if user is None:
        return None, []
    spaces = list(
        db.scalars(
            select(Space)
            .join(Membership, Membership.space_id == Space.id)
            .where(Membership.user_id == user.id)
            .order_by(Space.name, Space.id)
        )
    )
    return user, spaces


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user, spaces = load_session(request, db)
    if user is None:
        raise NotAuthenticated()
    request.state.spaces = spaces
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=404, detail="Страница не найдена.")
    return user


# Compared to the session user only. Do not render this value.
KINSHIP_EDITOR_EMAIL = "aharodnik@gmail.com"


def can_edit_kinship(user: User) -> bool:
    return (user.email or "").strip().lower() == KINSHIP_EDITOR_EMAIL


def require_space(
    space_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> Space:
    space = db.get(Space, space_id)
    member = None
    if space is not None:
        member = db.scalar(
            select(Membership).where(Membership.user_id == user.id, Membership.space_id == space.id)
        )
    if space is None or member is None:
        raise HTTPException(status_code=404, detail="Пространство не найдено.")
    request.state.spaces = getattr(request.state, "spaces", [])
    return space


def get_tree(db: Session, space_id: int, tree_id: int) -> Tree:
    tree = db.get(Tree, tree_id)
    if tree is None or tree.space_id != space_id:
        raise HTTPException(status_code=404, detail="Дерево не найдено.")
    return tree


def get_person(db: Session, space_id: int, person_id: int) -> tuple[Person, Tree]:
    person = db.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Человек не найден.")
    tree = db.get(Tree, person.tree_id)
    if tree is None or tree.space_id != space_id:
        raise HTTPException(status_code=404, detail="Человек не найден.")
    return person, tree


def get_dataset(db: Session, space_id: int, dataset_id: int) -> Dataset:
    dataset = db.get(Dataset, dataset_id)
    if dataset is None or dataset.space_id != space_id:
        raise HTTPException(status_code=404, detail="Таблица не найдена.")
    return dataset


def get_link(db: Session, space_id: int, link_id: int) -> PersonLink:
    link = db.get(PersonLink, link_id)
    if link is None or link.space_id != space_id:
        raise HTTPException(status_code=404, detail="Связь не найдена.")
    return link
