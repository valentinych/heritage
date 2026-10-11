from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.deps import can_edit_kinship, get_db, get_person, require_space, require_user
from app.importers.gedmatch import GedMatchError, parse_kit_owner, parse_one_to_many
from app.models import GedMatchHit, GedMatchKinship, GedMatchMatrix, GedMatchMatrixKit, Space, User
from app.services.gedmatch_cluster_tree import scheme_for_rows
from app.services.gedmatch_predict import (
    PARENT_LABEL,
    RANGES,
    apply_manual,
    assign_matrix_kit,
    choose_matrix,
    cluster_match,
    cluster_matrix,
    correspond,
    correspondence_matrix,
    forecast_for_pair,
    header_names_for,
    kinship_form_options,
    kits_for_matrix,
    list_matrices,
    measures_from_directed,
    parent_hint_lines,
    replace_hits,
    store_kinship_choice,
    stored_kinship_label,
)
from app.templating import redirect, render
from app.util import utcnow

router = APIRouter()


def _kits(db: Session, space_id: int) -> list[str]:
    return list(
        db.scalars(
            select(GedMatchHit.source_kit)
            .where(GedMatchHit.space_id == space_id)
            .distinct()
            .order_by(GedMatchHit.source_kit)
        )
    )


def _selection(db: Session, space_id: int, matrix_id: str):
    matrices = list_matrices(db, space_id)
    current = choose_matrix(matrices, matrix_id)
    only = kits_for_matrix(db, current) if current is not None else None
    return matrices, current, only


def _clean_kit(value: str) -> str:
    return value.strip().upper()


def _pair_kits(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left < right else (right, left)


def _parent_band() -> tuple[int, int]:
    for label, low, high in RANGES:
        if label == PARENT_LABEL:
            return low, high
    raise KeyError(PARENT_LABEL)


def _hit_rows(db: Session, space_id: int, *criteria):
    return db.execute(
        select(
            GedMatchHit.source_kit,
            GedMatchHit.match_kit,
            GedMatchHit.total_cm,
            GedMatchHit.generations,
        ).where(GedMatchHit.space_id == space_id, *criteria)
    ).all()


def _store_hits(directed_cm: dict, directed_gen: dict, rows) -> None:
    for source, match, cm, gen in rows:
        directed_cm[(source, match)] = cm
        directed_gen[(source, match)] = gen


def _parent_partners(directed_cm: dict, origin: str, low: int, high: int) -> set[str]:
    found: set[str] = set()
    for (left, right), cm in directed_cm.items():
        if cm is None or not (low <= cm <= high):
            continue
        if left == origin:
            found.add(right)
        elif right == origin:
            found.add(left)
    return found


def _parent_measures(db: Session, space_id: int):
    """Hints for the cluster page. Loading every hit exhausts the host."""
    low, high = _parent_band()
    directed_cm: dict[tuple[str, str], float | None] = {}
    directed_gen: dict[tuple[str, str], float | None] = {}
    _store_hits(
        directed_cm,
        directed_gen,
        _hit_rows(db, space_id, GedMatchHit.total_cm >= low, GedMatchHit.total_cm <= high),
    )
    needed: set[str] = set()
    seen: set[str] = set()
    for left, right in directed_cm:
        for origin in (left, right):
            if origin in seen:
                continue
            seen.add(origin)
            pals = _parent_partners(directed_cm, origin, low, high)
            if len(pals) >= 2:
                needed.update(pals)
    if len(needed) >= 2:
        _store_hits(
            directed_cm,
            directed_gen,
            _hit_rows(
                db,
                space_id,
                GedMatchHit.source_kit.in_(needed),
                GedMatchHit.match_kit.in_(needed),
            ),
        )
    return measures_from_directed(directed_cm, directed_gen)


def _measures_for_pair(db: Session, space_id: int, source: str, match: str):
    """Pair forecast without reading every hit. A full scan exhausts the host."""
    low, high = _parent_band()
    directed_cm: dict[tuple[str, str], float | None] = {}
    directed_gen: dict[tuple[str, str], float | None] = {}
    _store_hits(
        directed_cm,
        directed_gen,
        _hit_rows(
            db,
            space_id,
            or_(
                and_(GedMatchHit.source_kit == source, GedMatchHit.match_kit == match),
                and_(GedMatchHit.source_kit == match, GedMatchHit.match_kit == source),
            ),
        ),
    )
    _store_hits(
        directed_cm,
        directed_gen,
        _hit_rows(
            db,
            space_id,
            and_(GedMatchHit.total_cm >= low, GedMatchHit.total_cm <= high),
            or_(
                GedMatchHit.source_kit.in_((source, match)),
                GedMatchHit.match_kit.in_((source, match)),
            ),
        ),
    )
    for origin in (source, match):
        pals = _parent_partners(directed_cm, origin, low, high)
        if len(pals) < 2:
            continue
        _store_hits(
            directed_cm,
            directed_gen,
            _hit_rows(
                db,
                space_id,
                GedMatchHit.source_kit.in_(pals),
                GedMatchHit.match_kit.in_(pals),
            ),
        )
    return measures_from_directed(directed_cm, directed_gen)


def _pair_href(db: Session, space_id: int, kit_low: str, kit_high: str) -> str:
    forward = db.scalar(
        select(GedMatchHit.id).where(
            GedMatchHit.space_id == space_id,
            GedMatchHit.source_kit == kit_low,
            GedMatchHit.match_kit == kit_high,
        )
    )
    source, match = (kit_low, kit_high) if forward is not None else (kit_high, kit_low)
    return f"/s/{space_id}/gedmatch/clusters/{quote(source, safe='')}/{quote(match, safe='')}"


def _kinship_catalog(db: Session, space_id: int) -> list[dict]:
    rows = list(
        db.scalars(
            select(GedMatchKinship)
            .where(GedMatchKinship.space_id == space_id)
            .order_by(GedMatchKinship.kit_low, GedMatchKinship.kit_high)
        )
    )
    kits = [kit for row in rows for kit in (row.kit_low, row.kit_high)]
    names = header_names_for(db, space_id, kits)
    return [
        {
            "href": _pair_href(db, space_id, row.kit_low, row.kit_high),
            "label": stored_kinship_label(row.relation, row.kit_low, row.kit_high, row.parent_kit, names),
        }
        for row in rows
    ]


def _find_kinship(db: Session, space_id: int, source: str, match: str) -> GedMatchKinship | None:
    low, high = _pair_kits(source, match)
    return db.scalar(
        select(GedMatchKinship).where(
            GedMatchKinship.space_id == space_id,
            GedMatchKinship.kit_low == low,
            GedMatchKinship.kit_high == high,
        )
    )


def _require_kinship_editor(user: User) -> None:
    if not can_edit_kinship(user):
        raise HTTPException(status_code=403, detail="Недостаточно прав.")


def _known_pair(db: Session, space_id: int, source: str, match: str) -> bool:
    return cluster_match(db, space_id, source, match) is not None or cluster_match(
        db, space_id, match, source
    ) is not None


@router.get("/s/{space_id}/gedmatch")
def gedmatch_page(
    request: Request,
    kit: str = "",
    matrix: str = "",
    show: str = "",
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    kits = _kits(db, space.id)
    chosen = kit.strip().upper() or (kits[0] if kits else "")
    matrices, current, only = _selection(db, space.id, matrix)
    show_tables = show == "1"
    if show_tables and chosen:
        report = correspond(db, space.id, chosen)
    else:
        report = {"rows": [], "dense": [], "total": 0, "owner": None}
    grid = correspondence_matrix(db, space.id, only) if show_tables else {"kits": [], "rows": []}
    return render(
        request,
        "gedmatch.html",
        user=user,
        space=space,
        section="gedmatch",
        kits=kits,
        kit=chosen,
        rows=report["rows"],
        dense=report["dense"],
        total=report["total"],
        owner=report.get("owner"),
        matrix=grid,
        matrices=matrices,
        current=current,
        stored_kits=kits,
        show_tables=show_tables,
        kinships=_kinship_catalog(db, space.id),
    )


@router.post("/s/{space_id}/gedmatch/matrices")
def create_gedmatch_matrix(
    request: Request,
    name: str = Form(""),
    kit: list[str] = Form(default=[]),
    space: Space = Depends(require_space),
    _user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    cleaned = " ".join(name.split())
    if not cleaned:
        return redirect(request, f"/s/{space.id}/gedmatch", "Укажите имя матрицы.", "error")
    taken = db.scalar(
        select(GedMatchMatrix.id).where(
            GedMatchMatrix.space_id == space.id,
            GedMatchMatrix.name == cleaned,
        )
    )
    if taken is not None:
        return redirect(request, f"/s/{space.id}/gedmatch", "Матрица с таким именем уже есть.", "error")
    stored = set(_kits(db, space.id))
    picked: list[str] = []
    for item in kit:
        code = item.strip().upper()
        if code in stored and code not in picked:
            picked.append(code)
    row = GedMatchMatrix(space_id=space.id, name=cleaned[:200], receives_unlisted=False)
    db.add(row)
    db.flush()
    for code in picked:
        db.add(GedMatchMatrixKit(matrix_id=row.id, kit=code))
    db.commit()
    return redirect(request, f"/s/{space.id}/gedmatch?matrix={row.id}", "Матрица создана.")


@router.post("/s/{space_id}/gedmatch/matrices/join")
async def join_gedmatch_matrix(
    request: Request,
    space: Space = Depends(require_space),
    _user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    payload = await request.json()
    name = str(payload.get("name") or "")
    kit = str(payload.get("kit") or "")
    if not " ".join(name.split()) or not kit.strip():
        raise HTTPException(status_code=400, detail="Нужны name и kit.")
    assign_matrix_kit(db, space.id, name, kit)
    return {"ok": True}


@router.get("/s/{space_id}/gedmatch/clusters")
def gedmatch_clusters(
    request: Request,
    matrix: str = "",
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    matrices, current, only = _selection(db, space.id, matrix)
    grid = cluster_matrix(db, space.id, only)
    scheme = scheme_for_rows(db, space.id, grid["rows"], space.cluster_self or "")
    return render(
        request,
        "gedmatch_clusters.html",
        user=user,
        space=space,
        section="gedmatch",
        rows=grid["rows"],
        cluster_count=grid["cluster_count"],
        kit_count=grid["kit_count"],
        strong_cm=grid["strong_cm"],
        matrices=matrices,
        current=current,
        scheme=scheme,
        parent_hints=parent_hint_lines(_parent_measures(db, space.id)),
    )


@router.get("/s/{space_id}/gedmatch/clusters/{source_kit}/{match_kit}")
def gedmatch_cluster_pair(
    request: Request,
    source_kit: str,
    match_kit: str,
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    match = cluster_match(db, space.id, source_kit, match_kit)
    if match is None:
        raise HTTPException(status_code=404, detail="Совпадение не найдено.")
    forecast, names = forecast_for_pair(
        db,
        space.id,
        _measures_for_pair(db, space.id, match["source_kit"], match["match_kit"]),
        match["source_kit"],
        match["match_kit"],
    )
    saved = None
    saved_choice = ""
    row = _find_kinship(db, space.id, match["source_kit"], match["match_kit"])
    if row is not None:
        merged = apply_manual(
            forecast, row.relation, row.parent_kit, match["source_kit"], match["match_kit"], names
        )
        saved = {"sentence": merged["saved_sentence"]}
        saved_choice = merged["saved_choice"]
    return render(
        request,
        "gedmatch_cluster_pair.html",
        user=user,
        space=space,
        section="gedmatch",
        match=match,
        forecast=forecast,
        saved=saved,
        saved_choice=saved_choice,
        kinship_options=kinship_form_options(match["source_kit"], match["match_kit"], names),
        kinship_editor=can_edit_kinship(user),
    )


@router.post("/s/{space_id}/gedmatch/kinship")
def save_gedmatch_kinship(
    request: Request,
    source_kit: str = Form(""),
    match_kit: str = Form(""),
    relation: str = Form(""),
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    _require_kinship_editor(user)
    source = _clean_kit(source_kit)
    match = _clean_kit(match_kit)
    back = f"/s/{space.id}/gedmatch"
    if not source or not match or source == match or not _known_pair(db, space.id, source, match):
        return redirect(request, back, "Укажите два разных набора из совпадений.", "error")
    stored_choice = store_kinship_choice(relation, source, match)
    if stored_choice is None:
        return redirect(
            request,
            f"/s/{space.id}/gedmatch/clusters/{quote(source, safe='')}/{quote(match, safe='')}",
            "Неизвестная связь.",
            "error",
        )
    stored, parent_kit = stored_choice
    low, high = _pair_kits(source, match)
    row = _find_kinship(db, space.id, source, match)
    if row is None:
        row = GedMatchKinship(
            space_id=space.id,
            kit_low=low,
            kit_high=high,
            relation=stored,
            parent_kit=parent_kit,
            created_at=utcnow(),
        )
        db.add(row)
    else:
        row.relation = stored
        row.parent_kit = parent_kit
    db.commit()
    return redirect(
        request,
        f"/s/{space.id}/gedmatch/clusters/{quote(source, safe='')}/{quote(match, safe='')}",
        "Связь сохранена.",
    )


@router.post("/s/{space_id}/gedmatch/kinship/delete")
def delete_gedmatch_kinship(
    request: Request,
    source_kit: str = Form(""),
    match_kit: str = Form(""),
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    _require_kinship_editor(user)
    source = _clean_kit(source_kit)
    match = _clean_kit(match_kit)
    row = _find_kinship(db, space.id, source, match) if source and match else None
    if row is None:
        return redirect(request, f"/s/{space.id}/gedmatch", "Такой связи нет.", "error")
    db.delete(row)
    db.commit()
    target = f"/s/{space.id}/gedmatch/clusters/{quote(source, safe='')}/{quote(match, safe='')}"
    if cluster_match(db, space.id, source, match) is None:
        target = f"/s/{space.id}/gedmatch"
    return redirect(request, target, "Связь удалена.")


@router.post("/s/{space_id}/gedmatch")
async def upload_gedmatch(
    request: Request,
    kit: str = Form(""),
    file: UploadFile = File(...),
    space: Space = Depends(require_space),
    _user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    raw = await file.read()
    if len(raw) > 8_000_000:
        raise HTTPException(status_code=400, detail="Файл слишком большой.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    try:
        rows = parse_one_to_many(text)
    except GedMatchError as exc:
        return redirect(request, f"/s/{space.id}/gedmatch", str(exc), "error")
    owner = parse_kit_owner(text)
    source = kit.strip().upper() or (owner or {}).get("kit") or "KIT"
    stored = replace_hits(db, space.id, source, rows, owner)
    return redirect(request, f"/s/{space.id}/gedmatch?kit={source}", f"Сохранено совпадений: {stored}.")


@router.post("/s/{space_id}/gedmatch/import")
async def import_gedmatch(
    request: Request,
    space: Space = Depends(require_space),
    _user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    payload = await request.json()
    source = str(payload.get("source_kit") or "").strip().upper()
    rows = payload.get("rows")
    owner = payload.get("owner")
    if not source or not isinstance(rows, list):
        raise HTTPException(status_code=400, detail="Нужны source_kit и rows.")
    if owner is not None and not isinstance(owner, dict):
        raise HTTPException(status_code=400, detail="Нужны source_kit и rows.")
    stored = replace_hits(db, space.id, source, rows, owner)
    return {"message": f"Сохранено совпадений: {stored}.", "stored": stored}


@router.post("/s/{space_id}/gedmatch/{hit_id}/link")
def link_hit(
    request: Request,
    hit_id: int,
    person_id: int = Form(0),
    matrix: str = Form(""),
    show: str = Form(""),
    space: Space = Depends(require_space),
    _user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    hit = db.get(GedMatchHit, hit_id)
    if hit is None or hit.space_id != space.id:
        raise HTTPException(status_code=404, detail="Совпадение не найдено.")
    if person_id:
        get_person(db, space.id, person_id)
        hit.person_id = person_id
    else:
        hit.person_id = None
    db.commit()
    query = f"kit={hit.source_kit}"
    if matrix.strip().isdigit():
        query += f"&matrix={matrix.strip()}"
    if show == "1":
        query += "&show=1"
    return redirect(request, f"/s/{space.id}/gedmatch?{query}", "Привязка сохранена.")
