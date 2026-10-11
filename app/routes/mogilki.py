from __future__ import annotations

import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from app.auth import LoginGuard
from app.services.mogilki import load_bundle, load_map
from app.templating import redirect, render

router = APIRouter()


def _client_key(request: Request) -> str:
    host = "unknown" if request.client is None else (request.client.host or "unknown")
    return "mogilki:" + host


def _same_secret(got: str, expected: str) -> bool:
    got_b = got.encode()
    expected_b = expected.encode()
    if len(got_b) != len(expected_b):
        secrets.compare_digest(expected_b, expected_b)
        return False
    return secrets.compare_digest(got_b, expected_b)


def _unlocked(request: Request) -> bool:
    return request.session.get("mogilki") is True


def _return_path(request: Request) -> str:
    """Keep the cemetery filter across the password POST."""
    cemetery = (request.query_params.get("cemetery") or "").strip()
    if not cemetery:
        return "/mogilki"
    return "/mogilki?" + urlencode({"cemetery": cemetery})


@router.get("/mogilki")
def mogilki_page(request: Request):
    if not _unlocked(request):
        return render(request, "mogilki.html", unlocked=False, gate_action=_return_path(request))
    return render(request, "mogilki.html", unlocked=True)


@router.post("/mogilki")
def mogilki_login(request: Request, password: str = Form("")):
    guard: LoginGuard = request.app.state.login_guard
    key = _client_key(request)
    gate_action = _return_path(request)
    if guard.too_many(key):
        return render(
            request,
            "mogilki.html",
            status_code=429,
            unlocked=False,
            gate_action=gate_action,
            error="Занадта шмат спроб. Пачакайце і паспрабуйце яшчэ раз.",
        )
    expected = request.app.state.settings.mogilki_password
    if not expected or not _same_secret(password, expected):
        guard.record_failure(key)
        return render(
            request,
            "mogilki.html",
            status_code=401,
            unlocked=False,
            gate_action=gate_action,
            error="Няправільны пароль.",
        )
    guard.clear(key)
    request.session["mogilki"] = True
    return redirect(request, gate_action)


@router.post("/mogilki/logout")
def mogilki_logout(request: Request):
    request.session.pop("mogilki", None)
    return redirect(request, "/mogilki")


@router.get("/mogilki/data")
def mogilki_data(request: Request):
    if not _unlocked(request):
        return JSONResponse({"detail": "Патрэбны пароль."}, status_code=401)
    return load_bundle()


@router.get("/mogilki/map")
def mogilki_map(request: Request):
    if not _unlocked(request):
        return JSONResponse({"detail": "Патрэбны пароль."}, status_code=401)
    return load_map()
