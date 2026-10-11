from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import StrictUndefined
from starlette.responses import RedirectResponse

from app.models import LINK_CONFIRMED, LINK_PROPOSED, LINK_REJECTED

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

STATUS_LABEL = {
    LINK_PROPOSED: "предложено",
    LINK_CONFIRMED: "подтверждено",
    LINK_REJECTED: "отклонено",
}


def rusdate(value) -> str:
    if value is None or not hasattr(value, "strftime"):
        return ""
    if getattr(value, "tzinfo", None) is None:
        from datetime import timezone

        value = value.replace(tzinfo=timezone.utc)
    return value.strftime("%d.%m.%Y")


def format_cm(value) -> str:
    if value is None or value == "":
        return "—"
    number = float(value)
    text = f"{number:.1f}"
    if text.endswith(".0"):
        return text[:-2]
    return text


TEMPLATES.env.filters["rusdate"] = rusdate
TEMPLATES.env.filters["cm"] = format_cm
TEMPLATES.env.filters["status_label"] = lambda value: STATUS_LABEL.get(value, value)
TEMPLATES.env.undefined = StrictUndefined


def flash(request: Request, message: str, kind: str = "info") -> None:
    bag = list(request.session.get("flash", []))
    bag.append({"message": message, "kind": kind})
    request.session["flash"] = bag


def pop_flash(request: Request) -> list[dict[str, str]]:
    bag = request.session.pop("flash", [])
    return list(bag)


def render(request: Request, template: str, *, status_code: int = 200, **context):
    if "spaces" not in context:
        context["spaces"] = getattr(request.state, "spaces", [])
    context.setdefault("user", None)
    context.setdefault("space", None)
    context.setdefault("section", "")
    context.setdefault("error", None)
    context["flashes"] = pop_flash(request)
    return TEMPLATES.TemplateResponse(request, template, context, status_code=status_code)


def redirect(request: Request, url: str, message: str | None = None, kind: str = "info"):
    if message:
        flash(request, message, kind)
    return RedirectResponse(url, status_code=303)
