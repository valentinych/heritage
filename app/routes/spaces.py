from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.auth import hash_password, password_error, unset_password_hash, validate_username
from app.deps import get_db, require_admin, require_space, require_user
from app.models import LINK_PROPOSED, ClusterMember, DnaMatch, Membership, PersonLink, Space, Tree, User
from app.services.cleanup import remove_space, remove_user
from app.templating import redirect, render
from app.util import utcnow

router = APIRouter()


def _member_map(db: Session, spaces: list[Space]) -> dict[int, set[int]]:
    mapping: dict[int, set[int]] = {}
    for space in spaces:
        ids = db.scalars(select(Membership.user_id).where(Membership.space_id == space.id)).all()
        mapping[space.id] = set(ids)
    return mapping


def _admin_context(db: Session) -> dict[str, object]:
    accounts = list(db.scalars(select(User).order_by(User.username)))
    all_spaces = list(db.scalars(select(Space).order_by(Space.name, Space.id)))
    return {"accounts": accounts, "all_spaces": all_spaces, "member_map": _member_map(db, all_spaces)}


@router.get("/")
def home(request: Request):
    return render(request, "home.html", section="home")


@router.get("/go")
def go_to_space(
    request: Request,
    space_id: int,
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
):
    return redirect(request, f"/s/{space.id}")


@router.get("/s/{space_id}")
def overview(
    request: Request,
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    trees = db.scalar(select(func.count()).select_from(Tree).where(Tree.space_id == space.id)) or 0
    matches = db.scalar(select(func.count()).select_from(DnaMatch).where(DnaMatch.space_id == space.id)) or 0
    clusters = (
        db.scalar(
            select(func.count()).select_from(
                select(ClusterMember.dataset_id, ClusterMember.cluster_label)
                .where(ClusterMember.space_id == space.id)
                .distinct()
                .subquery()
            )
        )
        or 0
    )
    proposed = (
        db.scalar(
            select(func.count())
            .select_from(PersonLink)
            .where(PersonLink.space_id == space.id, PersonLink.status == LINK_PROPOSED)
        )
        or 0
    )
    return render(
        request,
        "overview.html",
        user=user,
        space=space,
        section="overview",
        counts={"trees": trees, "matches": matches, "clusters": clusters, "proposed": proposed},
    )


@router.get("/s/{space_id}/sources")
def sources(
    request: Request,
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
):
    return render(request, "sources.html", user=user, space=space, section="sources")


@router.get("/admin")
def admin_page(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    return render(request, "admin.html", user=user, section="admin", **_admin_context(db))


@router.post("/admin/users")
def create_user(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    is_admin: str | None = Form(None),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    clean = validate_username(username)
    password = password.strip()
    error = password_error(password) if password else None
    if clean is None:
        error = "Имя может содержать буквы, цифры, точку, _ и -. Длина от 2 до 64."
    elif error is None and db.scalar(select(User).where(User.username == clean)):
        error = "Такое имя уже занято."
    if error:
        return render(
            request,
            "admin.html",
            status_code=400,
            user=user,
            section="admin",
            error=error,
            **_admin_context(db),
        )
    db.add(
        User(
            username=clean,
            password_hash=hash_password(password) if password else unset_password_hash(),
            must_set_password=not password,
            is_admin=is_admin is not None,
            created_at=utcnow(),
        )
    )
    db.commit()
    return redirect(request, "/admin", "Пользователь создан.")


@router.post("/admin/users/{user_id}/password")
def change_password(
    request: Request,
    user_id: int,
    password: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    account = db.get(User, user_id)
    error = password_error(password)
    if account is None:
        error = "Пользователь не найден."
    if error:
        return render(
            request,
            "admin.html",
            status_code=400,
            user=user,
            section="admin",
            error=error,
            **_admin_context(db),
        )
    account.password_hash = hash_password(password)
    account.must_set_password = False
    db.commit()
    return redirect(request, "/admin", "Пароль обновлён.")


@router.post("/admin/users/{user_id}/password-reset")
def reset_password(
    request: Request,
    user_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    account = db.get(User, user_id)
    if account is None:
        return render(
            request,
            "admin.html",
            status_code=400,
            user=user,
            section="admin",
            error="Пользователь не найден.",
            **_admin_context(db),
        )
    account.password_hash = unset_password_hash()
    account.must_set_password = True
    db.commit()
    return redirect(request, "/admin", "При следующем входе нужно задать пароль.")


@router.post("/admin/users/{user_id}/delete")
def delete_user(
    request: Request,
    user_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    account = db.get(User, user_id)
    if account is None:
        return render(
            request,
            "admin.html",
            status_code=404,
            user=user,
            section="admin",
            error="Пользователь не найден.",
            **_admin_context(db),
        )
    try:
        remove_user(db, user, account)
    except ValueError as exc:
        db.rollback()
        return render(
            request,
            "admin.html",
            status_code=400,
            user=user,
            section="admin",
            error=str(exc),
            **_admin_context(db),
        )
    return redirect(request, "/admin", "Пользователь удалён.")


@router.post("/admin/spaces")
def create_space(
    request: Request,
    name: str = Form(""),
    add_self: str | None = Form(None),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    clean = " ".join(name.split())
    error = None
    if not clean:
        error = "Введите название пространства."
    elif len(clean) > 80:
        error = "Название длиннее 80 символов."
    else:
        existing = db.scalars(select(Space.name)).all()
        if any(item.casefold() == clean.casefold() for item in existing):
            error = "Пространство с таким названием уже есть."
    if error:
        return render(
            request,
            "admin.html",
            status_code=400,
            user=user,
            section="admin",
            error=error,
            **_admin_context(db),
        )
    space = Space(name=clean, created_at=utcnow())
    db.add(space)
    db.flush()
    if add_self is not None:
        db.add(Membership(user_id=user.id, space_id=space.id))
    db.commit()
    return redirect(request, "/admin", "Пространство создано.")


@router.post("/admin/spaces/{space_id}/members")
async def save_members(
    request: Request,
    space_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    space = db.get(Space, space_id)
    if space is None:
        return render(
            request,
            "admin.html",
            status_code=404,
            user=user,
            section="admin",
            error="Пространство не найдено.",
            **_admin_context(db),
        )
    form = await request.form()
    wanted: set[int] = set()
    for raw in form.getlist("user_id"):
        if str(raw).isdigit():
            wanted.add(int(raw))
    known = set(db.scalars(select(User.id)))
    db.execute(delete(Membership).where(Membership.space_id == space.id))
    for user_id in wanted & known:
        db.add(Membership(user_id=user_id, space_id=space.id))
    db.commit()
    return redirect(request, "/admin", "Доступ сохранён.")


@router.post("/admin/spaces/{space_id}/delete")
def delete_space(
    request: Request,
    space_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    space = db.get(Space, space_id)
    if space is None:
        return render(
            request,
            "admin.html",
            status_code=404,
            user=user,
            section="admin",
            error="Пространство не найдено.",
            **_admin_context(db),
        )
    remove_space(db, request.app.state.settings.data_dir, space)
    return redirect(request, "/admin", "Пространство удалено.")
