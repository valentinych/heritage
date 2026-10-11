from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import (
    LoginGuard,
    hash_password,
    password_error,
    unset_password_hash,
    validate_username,
    verify_login,
)
from app.deps import get_db, load_session
from app.models import User
from app.templating import redirect, render
from app.util import utcnow

_USERNAME_ERROR = "Имя может содержать буквы, цифры, точку, _ и -. Длина от 2 до 64."
_DUPLICATE_USERNAME = "Такое имя уже занято."

router = APIRouter()


def _client_key(request: Request) -> str:
    if request.client is None:
        return "unknown"
    return request.client.host or "unknown"


@router.get("/login")
def login_form(request: Request, db: Session = Depends(get_db)):
    user, _spaces = load_session(request, db)
    if user is not None:
        return redirect(request, "/")
    count = db.scalar(select(User.id).limit(1))
    return render(request, "login.html", user=None, setup_needed=count is None)


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    guard: LoginGuard = request.app.state.login_guard
    key = _client_key(request)
    if guard.too_many(key):
        return render(
            request,
            "login.html",
            status_code=429,
            user=None,
            setup_needed=False,
            error="Слишком много попыток. Подождите и попробуйте снова.",
        )
    user = db.scalar(select(User).where(User.username == username.strip()))
    if user is not None and user.must_set_password:
        return render(
            request,
            "login.html",
            user=None,
            setup_needed=False,
            choose_password=True,
            username=user.username,
        )
    if not verify_login(user, password) or user is None:
        guard.record_failure(key)
        return render(
            request,
            "login.html",
            status_code=401,
            user=None,
            setup_needed=False,
            error="Неверное имя или пароль.",
        )
    guard.clear(key)
    request.session.clear()
    request.session["user_id"] = user.id
    return redirect(request, "/")


@router.post("/login/password")
def choose_password(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    password_repeat: str = Form(""),
    db: Session = Depends(get_db),
):
    user = db.scalar(select(User).where(User.username == username.strip()))
    if user is None or not user.must_set_password:
        return redirect(request, "/login")
    error = password_error(password)
    if error is None and password != password_repeat:
        error = "Пароли не совпадают."
    if error:
        return render(
            request,
            "login.html",
            status_code=400,
            user=None,
            setup_needed=False,
            choose_password=True,
            username=user.username,
            error=error,
        )
    user.password_hash = hash_password(password)
    user.must_set_password = False
    db.commit()
    request.session.clear()
    request.session["user_id"] = user.id
    return redirect(request, "/")


def _register_page(
    request: Request,
    *,
    username: str = "",
    error: str | None = None,
    status_code: int = 200,
):
    return render(
        request,
        "register.html",
        status_code=status_code,
        user=None,
        section="register",
        username=username,
        error=error,
    )


@router.get("/register")
def register_form(request: Request, db: Session = Depends(get_db)):
    user, _spaces = load_session(request, db)
    if user is not None:
        return redirect(request, "/")
    return _register_page(request)


@router.post("/register")
def register_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    clean = validate_username(username)
    error = None if clean is None else password_error(password)
    if clean is None:
        error = _USERNAME_ERROR
    elif error is None and db.scalar(select(User.id).where(User.username == clean)):
        error = _DUPLICATE_USERNAME
    if error:
        return _register_page(request, username=username.strip(), error=error, status_code=400)
    account = User(
        username=clean,
        password_hash=hash_password(password),
        must_set_password=False,
        is_admin=False,
        created_at=utcnow(),
    )
    db.add(account)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _register_page(request, username=clean or "", error=_DUPLICATE_USERNAME, status_code=400)
    request.session.clear()
    request.session["user_id"] = account.id
    return redirect(request, "/")


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect(request, "/login")
