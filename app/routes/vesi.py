from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.deps import get_db, load_session
from app.models import ArtifactSettlement, User
from app.services.vesi import get_artifact, public_map, unbound_artifacts
from app.templating import redirect, render

router = APIRouter()

_BIND_USER = "ruslan"


def _bindings(db: Session) -> dict[str, str]:
    return {row.artifact_id: row.settlement_id for row in db.scalars(select(ArtifactSettlement))}


def _can_bind(user: User | None) -> bool:
    return user is not None and user.username == _BIND_USER


@router.get("/vesi")
def vesi_page(request: Request, db: Session = Depends(get_db)):
    user, _spaces = load_session(request, db)
    bindings = _bindings(db)
    return render(
        request,
        "vesi.html",
        section="vesi",
        user=user,
        can_bind=_can_bind(user),
        unbound=unbound_artifacts(bindings),
    )


@router.get("/vesi/data")
def vesi_data(db: Session = Depends(get_db)):
    return public_map(_bindings(db))


@router.post("/vesi/bind")
def vesi_bind(
    request: Request,
    artifact_id: str = Form(""),
    settlement_id: str = Form(""),
    db: Session = Depends(get_db),
):
    user, _spaces = load_session(request, db)
    if not _can_bind(user):
        raise HTTPException(status_code=403, detail="Недостаточно прав.")
    bindings = _bindings(db)
    pending = {item["id"]: item for item in unbound_artifacts(bindings)}
    artifact = pending.get(artifact_id)
    allowed = {item["id"] for item in artifact["suggestions"]} if artifact and artifact["has_point"] else set()
    settlements = {item["id"]: item for item in public_map(bindings)["settlements"]}
    failed = "Не удалось привязать парафию." if artifact and artifact.get("kind") == "parish" else "Не удалось привязать кладбище."
    if artifact is None or settlement_id not in allowed or settlement_id not in settlements:
        return redirect(request, "/vesi", failed, "error")
    db.add(ArtifactSettlement(artifact_id=artifact_id, settlement_id=settlement_id))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return redirect(request, "/vesi", failed, "error")
    name = settlements[settlement_id]["name"]
    if artifact.get("kind") == "parish":
        return redirect(request, "/vesi", f"Парафия привязана к населённому пункту {name}.")
    return redirect(request, "/vesi", f"Кладбище привязано к населённому пункту {name}.")


@router.get("/vesi/{artifact_id}")
def vesi_artifact(request: Request, artifact_id: str, db: Session = Depends(get_db)):
    artifact = get_artifact(artifact_id, _bindings(db))
    if artifact is None:
        raise HTTPException(status_code=404, detail="Страница не найдена.")
    return render(
        request,
        "vesi_artifact.html",
        section="vesi",
        artifact=artifact,
        title=artifact["name"],
    )
