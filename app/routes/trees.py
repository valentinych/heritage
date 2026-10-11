from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import FileResponse

from app.config import Settings
from app.deps import get_db, get_person, get_tree, require_space, require_user
from app.importers.names import normalize_name
from app.models import Family, Person, Space, Tree, User, family_child
from app.services.cleanup import remove_tree
from app.services.gedcom_import import import_gedcom
from app.services.pedigree import layout_pedigree
from app.services.people import person_card
from app.services.storage import remove_file, resolve_path, save_bytes
from app.templating import redirect, render
from app.uploads import GEDCOM_SUFFIXES, read_upload

router = APIRouter()


def _trees(db: Session, space_id: int) -> list[Tree]:
    return list(db.scalars(select(Tree).where(Tree.space_id == space_id).order_by(Tree.created_at.desc(), Tree.id.desc())))


def _pedigree(db: Session, space_id: int, tree_id: int, query: str):
    people = list(db.scalars(select(Person).where(Person.tree_id == tree_id).order_by(Person.id)))
    families = list(db.scalars(select(Family).where(Family.tree_id == tree_id).order_by(Family.id)))
    children: dict[int, list[int]] = {family.id: [] for family in families}
    if families:
        rows = db.execute(
            select(family_child.c.family_id, family_child.c.person_id)
            .where(family_child.c.family_id.in_([family.id for family in families]))
            .order_by(family_child.c.position, family_child.c.person_id)
        ).all()
        for family_id, person_id in rows:
            children[family_id].append(person_id)
    tokens = [token.replace("%", "").replace("_", "") for token in normalize_name(query).split()]
    tokens = [token for token in tokens if token]
    if tokens:
        matched = [person.id for person in people if all(token in person.name_key for token in tokens)]
        keep = _focus(matched, families, children)
        if not keep:
            return None
        people = [person for person in people if person.id in keep]
    else:
        keep = None
    payload_people = [
        {
            "id": person.id,
            "name": person.full_name,
            "detail": _years(person),
            "href": f"/s/{space_id}/people/{person.id}",
            "sex": person.sex if person.sex in {"M", "F"} else "",
        }
        for person in people
    ]
    payload_families = []
    for family in families:
        child_ids = children[family.id]
        if keep is not None:
            child_ids = [person_id for person_id in child_ids if person_id in keep]
            if family.husband_id not in keep and family.wife_id not in keep and not child_ids:
                continue
        payload_families.append(
            {"husband_id": family.husband_id, "wife_id": family.wife_id, "child_ids": child_ids}
        )
    return layout_pedigree(payload_people, payload_families)


def _focus(matched: list[int], families: list[Family], children: dict[int, list[int]]) -> set[int]:
    parents: dict[int, list[int]] = {}
    kids: dict[int, list[int]] = {}
    for family in families:
        pair = [person_id for person_id in (family.husband_id, family.wife_id) if person_id]
        for child_id in children[family.id]:
            for parent_id in pair:
                parents.setdefault(child_id, []).append(parent_id)
                kids.setdefault(parent_id, []).append(child_id)
    keep = set(matched)
    stack = list(keep)
    while stack:
        current = stack.pop()
        for parent_id in parents.get(current, []):
            if parent_id not in keep:
                keep.add(parent_id)
                stack.append(parent_id)
    stack = list(keep)
    while stack:
        current = stack.pop()
        for child_id in kids.get(current, []):
            if child_id not in keep:
                keep.add(child_id)
                stack.append(child_id)
    for family in families:
        pair = [person_id for person_id in (family.husband_id, family.wife_id) if person_id]
        if any(person_id in keep for person_id in pair):
            keep.update(pair)
    return keep


def _years(person: Person) -> str:
    if person.birth_year and person.death_year:
        return f"{person.birth_year}–{person.death_year}"
    if person.birth_year:
        return f"р. {person.birth_year}"
    if person.death_year:
        return f"ум. {person.death_year}"
    return ""


@router.get("/s/{space_id}/trees")
def tree_list(
    request: Request,
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    return render(request, "trees.html", user=user, space=space, section="trees", trees=_trees(db, space.id))


@router.post("/s/{space_id}/trees")
async def upload_tree(
    request: Request,
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    file: UploadFile = File(),
):
    settings: Settings = request.app.state.settings
    try:
        filename, content = await read_upload(file, GEDCOM_SUFFIXES, "Нужен файл GEDCOM (.ged или .gedcom).")
    except ValueError as exc:
        return render(
            request,
            "trees.html",
            status_code=400,
            user=user,
            space=space,
            section="trees",
            trees=_trees(db, space.id),
            error=str(exc),
        )
    relative = save_bytes(settings.data_dir, space.id, filename, content)
    try:
        tree = import_gedcom(
            db,
            space_id=space.id,
            user_id=user.id,
            filename=filename,
            stored_path=relative,
            content=content,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        remove_file(settings.data_dir, relative)
        return render(
            request,
            "trees.html",
            status_code=400,
            user=user,
            space=space,
            section="trees",
            trees=_trees(db, space.id),
            error=str(exc),
        )
    except Exception:
        db.rollback()
        remove_file(settings.data_dir, relative)
        raise
    return redirect(request, f"/s/{space.id}/trees/{tree.id}", "Дерево загружено.")


@router.get("/s/{space_id}/trees/{tree_id}")
def tree_detail(
    request: Request,
    tree_id: int,
    q: str = "",
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    tree = get_tree(db, space.id, tree_id)
    scheme = _pedigree(db, space.id, tree.id, q)
    return render(
        request,
        "tree.html",
        user=user,
        space=space,
        section="trees",
        tree=tree,
        scheme=scheme,
        query=q,
    )


@router.post("/s/{space_id}/trees/{tree_id}/delete")
def delete_tree(
    request: Request,
    tree_id: int,
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    tree = get_tree(db, space.id, tree_id)
    remove_tree(db, request.app.state.settings.data_dir, tree)
    return redirect(request, f"/s/{space.id}/trees", "Дерево удалено.")


@router.get("/s/{space_id}/people/{person_id}")
def person_page(
    request: Request,
    person_id: int,
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    person, tree = get_person(db, space.id, person_id)
    card = person_card(db, person, tree)
    return render(
        request,
        "person.html",
        user=user,
        space=space,
        section="trees",
        person=person,
        tree=tree,
        **card,
    )


@router.get("/s/{space_id}/files/trees/{tree_id}")
def download_tree(
    request: Request,
    tree_id: int,
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    tree = get_tree(db, space.id, tree_id)
    try:
        path = resolve_path(request.app.state.settings.data_dir, tree.stored_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Файл не найден.") from None
    return FileResponse(path, filename=tree.filename, content_disposition_type="attachment")
