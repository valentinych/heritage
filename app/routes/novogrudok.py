from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from markupsafe import Markup, escape
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.deps import can_edit_kinship, get_db, load_session
from app.models import GedMatchHit, GedMatchKinship, Space, User
from app.services.gedmatch_cluster_tree import scheme_for_rows
from app.services.gedmatch_predict import (
    PARENT_LABEL,
    RANGES,
    apply_manual,
    cluster_match,
    cluster_matrix,
    forecast_for_pair,
    kinship_form_options,
    measures_from_directed,
    store_kinship_choice,
)
from app.services.novogrudok import SPACE_ID, load_column_kits, saved_page_count
from app.templating import redirect, render
from app.util import utcnow

router = APIRouter()


def _mirror_one_direction(grid: dict) -> None:
    """Show a match saved on only one page in both matrix cells."""
    by_kit = {row["kit"]: row for row in grid["rows"]}
    index = {kit: position for position, kit in enumerate(grid["kits"])}
    for row in grid["rows"]:
        kit = row["kit"]
        for other, cell in zip(grid["kits"], row["cells"]):
            if cell["known"] or kit == other:
                continue
            opposite = by_kit[other]["cells"][index[kit]]
            if not opposite["known"]:
                continue
            cell["css"] = opposite["css"]
            cell["gen"] = opposite["gen"]
            cell["cm"] = opposite["cm"]
            cell["known"] = True


def _public_match_links(grid: dict) -> None:
    order = grid["kits"]
    for row in grid["rows"]:
        for other, cell in zip(order, row["cells"]):
            if not cell["known"]:
                continue
            cell["href"] = (
                f"/novogrudok/{quote(row['kit'], safe='')}/{quote(other, safe='')}"
            )


@router.get("/novogrudok")
def novogrudok_page(
    request: Request,
    show: str = "",
    db: Session = Depends(get_db),
):
    kits = load_column_kits()
    grid = cluster_matrix(db, SPACE_ID, kits, linked=False, cell_color="gen")
    _mirror_one_direction(grid)
    _public_match_links(grid)
    space = db.get(Space, SPACE_ID)
    scheme = scheme_for_rows(
        db,
        SPACE_ID,
        grid["rows"],
        space.cluster_self if space is not None else "",
        link_people=False,
    )
    user, spaces = load_session(request, db)
    return render(
        request,
        "novogrudok.html",
        user=user,
        spaces=spaces,
        rows=grid["rows"],
        cluster_count=grid["cluster_count"],
        kit_count=grid["kit_count"],
        saved_count=saved_page_count(db, SPACE_ID, kits),
        show_cm=show.strip().lower() == "cm",
        scheme=scheme,
        section="novogrudok",
    )


def _clean_kit(value: str) -> str:
    return value.strip().upper()


def _pair_kits(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left < right else (right, left)


def _pair_url(source: str, match: str) -> str:
    if not source or not match:
        return "/novogrudok"
    return f"/novogrudok/{quote(source, safe='')}/{quote(match, safe='')}"


def _parent_band() -> tuple[int, int]:
    for label, low, high in RANGES:
        if label == PARENT_LABEL:
            return low, high
    raise KeyError(PARENT_LABEL)


def _hit_rows(db: Session, *criteria):
    return db.execute(
        select(
            GedMatchHit.source_kit,
            GedMatchHit.match_kit,
            GedMatchHit.total_cm,
            GedMatchHit.generations,
        ).where(GedMatchHit.space_id == SPACE_ID, *criteria)
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


def _measures_for_pair(db: Session, source: str, match: str):
    """Pair forecast without reading every hit. A full scan of this public page can exhaust the host."""
    low, high = _parent_band()
    directed_cm: dict[tuple[str, str], float | None] = {}
    directed_gen: dict[tuple[str, str], float | None] = {}
    _store_hits(
        directed_cm,
        directed_gen,
        _hit_rows(
            db,
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
                GedMatchHit.source_kit.in_(pals),
                GedMatchHit.match_kit.in_(pals),
            ),
        )
    return measures_from_directed(directed_cm, directed_gen)


def _find_kinship(db: Session, source: str, match: str) -> GedMatchKinship | None:
    low, high = _pair_kits(source, match)
    return db.scalar(
        select(GedMatchKinship).where(
            GedMatchKinship.space_id == SPACE_ID,
            GedMatchKinship.kit_low == low,
            GedMatchKinship.kit_high == high,
        )
    )


def _known_pair(db: Session, source: str, match: str) -> bool:
    return cluster_match(db, SPACE_ID, source, match) is not None or cluster_match(
        db, SPACE_ID, match, source
    ) is not None


_GEDMATCH_KIT = "https://app.gedmatch.com/OneToManyFreeOriginalResults.php?kit="


def _drop_kit_names(text: str, names: dict[str, str]) -> str:
    """Public pages name a kit by its code. A display name sits in parentheses after that code."""
    cleaned = text
    labeled = sorted({(name or "").strip() for name in names.values() if (name or "").strip()}, key=len, reverse=True)
    for name in labeled:
        cleaned = cleaned.replace(f" ({name})", "")
    return cleaned


def _link_kit_codes(text: str, kits: list[str]) -> Markup:
    ordered = sorted({kit for kit in kits if kit}, key=len, reverse=True)
    if not text or not ordered:
        return Markup(escape(text or ""))
    pattern = re.compile("|".join(re.escape(kit) for kit in ordered))
    chunks: list[Markup] = []
    cursor = 0
    for found in pattern.finditer(text):
        chunks.append(escape(text[cursor:found.start()]))
        kit = found.group(0)
        href = escape(f"{_GEDMATCH_KIT}{quote(kit, safe='')}")
        chunks.append(Markup(f'<a href="{href}">{escape(kit)}</a>'))
        cursor = found.end()
    chunks.append(escape(text[cursor:]))
    return Markup("").join(chunks)


def _kinship_view(db: Session, source: str, match: str) -> tuple[dict, dict | None, str]:
    forecast, names = forecast_for_pair(db, SPACE_ID, _measures_for_pair(db, source, match), source, match)
    public = dict(forecast)
    public["summary"] = _drop_kit_names(forecast["summary"], names)
    kits = list(dict.fromkeys([*names, source, match]))
    saved = None
    saved_choice = ""
    row = _find_kinship(db, source, match)
    if row is not None:
        merged = apply_manual(public, row.relation, row.parent_kit, source, match, None)
        saved = {"sentence": _link_kit_codes(merged["saved_sentence"], kits)}
        saved_choice = merged["saved_choice"]
    public["summary"] = _link_kit_codes(public["summary"], kits)
    return public, saved, saved_choice


def _editor(user: User | None) -> bool:
    return user is not None and can_edit_kinship(user)


_NOT_COMPARED = "Эти наборы не сравнивались, прогноз родства не строится."


def _empty_match(source: str, match: str) -> dict:
    return {
        "source_kit": source,
        "match_kit": match,
        "source_name": "",
        "name": "",
        "total_cm": None,
        "largest_cm": None,
        "generations": None,
        "overlap": None,
        "date_compared": "",
        "company": "",
    }


def _require_editor(user: User | None) -> None:
    if not _editor(user):
        raise HTTPException(status_code=403, detail="Недостаточно прав.")


@router.get("/novogrudok/{source_kit}/{match_kit}")
def novogrudok_pair(
    request: Request,
    source_kit: str,
    match_kit: str,
    db: Session = Depends(get_db),
):
    source = _clean_kit(source_kit)
    other = _clean_kit(match_kit)
    if not source or not other or source == other:
        raise HTTPException(status_code=404, detail="Совпадение не найдено.")
    match = cluster_match(db, SPACE_ID, source, other) or cluster_match(db, SPACE_ID, other, source)
    user, spaces = load_session(request, db)
    if match is None:
        return render(
            request,
            "novogrudok_pair.html",
            user=user,
            spaces=spaces,
            match=_empty_match(source, other),
            forecast={"summary": _NOT_COMPARED},
            saved=None,
            saved_choice="",
            kinship_options=[],
            kinship_editor=False,
            section="novogrudok",
        )
    forecast, saved, saved_choice = _kinship_view(db, match["source_kit"], match["match_kit"])
    return render(
        request,
        "novogrudok_pair.html",
        user=user,
        spaces=spaces,
        match=match,
        forecast=forecast,
        saved=saved,
        saved_choice=saved_choice,
        kinship_options=kinship_form_options(match["source_kit"], match["match_kit"]),
        kinship_editor=_editor(user),
        section="novogrudok",
    )


@router.post("/novogrudok/kinship")
def save_novogrudok_kinship(
    request: Request,
    source_kit: str = Form(""),
    match_kit: str = Form(""),
    relation: str = Form(""),
    db: Session = Depends(get_db),
):
    user, _spaces = load_session(request, db)
    _require_editor(user)
    source = _clean_kit(source_kit)
    match = _clean_kit(match_kit)
    back = _pair_url(source, match)
    if not source or not match or source == match or not _known_pair(db, source, match):
        return redirect(request, "/novogrudok", "Укажите два разных набора из совпадений.", "error")
    stored_choice = store_kinship_choice(relation, source, match)
    if stored_choice is None:
        return redirect(request, back, "Неизвестная связь.", "error")
    stored, parent_kit = stored_choice
    low, high = _pair_kits(source, match)
    row = _find_kinship(db, source, match)
    if row is None:
        row = GedMatchKinship(
            space_id=SPACE_ID,
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
    return redirect(request, back, "Связь сохранена.")


@router.post("/novogrudok/kinship/delete")
def delete_novogrudok_kinship(
    request: Request,
    source_kit: str = Form(""),
    match_kit: str = Form(""),
    db: Session = Depends(get_db),
):
    user, _spaces = load_session(request, db)
    _require_editor(user)
    source = _clean_kit(source_kit)
    match = _clean_kit(match_kit)
    row = _find_kinship(db, source, match) if source and match else None
    if row is None:
        return redirect(request, _pair_url(source, match), "Такой связи нет.", "error")
    db.delete(row)
    db.commit()
    target = _pair_url(source, match)
    if cluster_match(db, SPACE_ID, source, match) is None:
        target = "/novogrudok"
    return redirect(request, target, "Связь удалена.")
