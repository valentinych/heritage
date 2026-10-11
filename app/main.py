from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse, RedirectResponse

from app.auth import LoginGuard, ensure_admin
from app.config import Settings, normalize_settings
from app.db import create_tables, make_engine, make_session_factory
from app.deps import NotAuthenticated, load_session
from app.routes.auth import router as auth_router
from app.routes.datasets import router as dataset_router
from app.routes.gedmatch import router as gedmatch_router
from app.routes.mogilki import router as mogilki_router
from app.routes.novogrudok import router as novogrudok_router
from app.routes.spaces import router as space_router
from app.routes.vesi import router as vesi_router
from app.services.vesi import ensure_artifact_settlements, load_parishes, load_prikhody
from app.routes.trees import router as tree_router
from app.templating import render

STATIC_DIR = Path(__file__).resolve().parent / "static"
VENDOR_DIR = Path(__file__).resolve().parents[1] / "maps" / "cemeteries" / "vendor"


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings.from_env()
    settings = normalize_settings(settings)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings.data_dir.mkdir(parents=True, exist_ok=True)
        (app.state.settings.data_dir / "uploads").mkdir(parents=True, exist_ok=True)
        create_tables(app.state.engine)
        db = app.state.session_factory()
        try:
            load_parishes()
            load_prikhody()
            ensure_artifact_settlements(db)
            ensure_admin(db, app.state.settings)
        finally:
            db.close()
        yield

    app = FastAPI(title="Наследие", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    engine = make_engine(settings.data_dir / "heritage.db")
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.login_guard = LoginGuard()

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        mogilki = request.url.path == "/mogilki" or request.url.path.startswith("/mogilki/")
        vesi_map = request.url.path == "/vesi"
        # OpenStreetMap rejects tiles that arrive without a Referer.
        response.headers["Referrer-Policy"] = (
            "strict-origin-when-cross-origin" if mogilki or vesi_map else "same-origin"
        )
        if mogilki or vesi_map:
            policy = (
                "default-src 'self'; style-src 'self'; "
                "img-src 'self' https://tile.openstreetmap.org; "
                "form-action 'self'; frame-ancestors 'none'; base-uri 'self'; script-src 'self'"
            )
        else:
            policy = (
                "default-src 'self'; style-src 'self'; img-src 'self'; "
                "form-action 'self'; frame-ancestors 'none'; base-uri 'self'; script-src 'none'"
            )
        response.headers["Content-Security-Policy"] = policy
        return response

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret,
        session_cookie="heritage_session",
        max_age=14 * 24 * 3600,
        same_site="lax",
        https_only=settings.cookie_secure,
    )

    @app.exception_handler(NotAuthenticated)
    async def unauthenticated(request, exc):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc: StarletteHTTPException):
        if exc.status_code != 404:
            detail = exc.detail if isinstance(exc.detail, (str, dict, list)) else "Ошибка"
            return JSONResponse({"detail": detail}, status_code=exc.status_code)
        message = exc.detail if isinstance(exc.detail, str) else "Страница не найдена."
        db = request.app.state.session_factory()
        try:
            user, spaces = load_session(request, db)
        finally:
            db.close()
        return render(
            request,
            "error.html",
            status_code=404,
            user=user,
            spaces=spaces,
            title="Не найдено",
            message=message,
        )

    @app.get("/health")
    def health():
        return {"ok": True}

    app.include_router(auth_router)
    app.include_router(space_router)
    app.include_router(tree_router)
    app.include_router(dataset_router)
    app.include_router(gedmatch_router)
    app.include_router(novogrudok_router)
    app.include_router(vesi_router)
    app.include_router(mogilki_router)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.mount("/mogilki/vendor", StaticFiles(directory=str(VENDOR_DIR)), name="mogilki-vendor")
    return app


app = create_app()
