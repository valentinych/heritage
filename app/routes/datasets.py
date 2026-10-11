from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, aliased
from starlette.responses import FileResponse

from app.deps import get_dataset, get_db, get_link, require_space, require_user
from app.importers.geneteka import GenetekaError, fetch_parish, load_from_payload, local_hint
from app.importers.autocluster import AutoCluster, parse_autocluster
from app.importers.tables import FIELDS, guess_mapping, parse_mapping, read_table
from app.services.cluster_branches import analyze_autocluster, build_branches
from app.models import (
    KIND_CLUSTERS,
    KIND_LINKS,
    KIND_MATCHES,
    LINK_STATUSES,
    ClusterMember,
    ClusterPair,
    Dataset,
    DnaMatch,
    Person,
    PersonLink,
    Space,
    Tree,
    User,
)
from app.services.cleanup import remove_dataset
from app.services.cluster_self import person_key, split_for_self
from app.services.cluster_tree import layout_branches, layout_cluster_tree, layout_self_sides
from app.services.geneteka_import import store_geneteka
from app.services.storage import remove_file, resolve_path, save_bytes
from app.services.link_trees import PEOPLE_PAGE, assemble_link_trees, assembled_people
from app.services.matching import dataset_link_report, match_dataset
from app.services.table_import import replace_imported
from app.templating import redirect, render
from app.uploads import TABLE_SUFFIXES, read_upload

CLUSTER_SUFFIXES = TABLE_SUFFIXES | {".html", ".htm"}
from app.util import paginate, pager, utcnow

router = APIRouter()

BACK = {
    KIND_MATCHES: ("dna", "К матчам ДНК"),
    KIND_CLUSTERS: ("clusters", "К кластерам"),
    KIND_LINKS: ("links", "К связям"),
}

STATUS_FLASH = {
    "confirmed": "Связь подтверждена.",
    "rejected": "Связь отклонена.",
    "proposed": "Связь возвращена в предложенные.",
}

EMPTY_LINKS = {
    "proposed": "Предложенных связей нет.",
    "confirmed": "Подтверждённых связей нет.",
    "rejected": "Отклонённых связей нет.",
}


def _datasets(db: Session, space_id: int, kind: str) -> list[Dataset]:
    return list(
        db.scalars(
            select(Dataset)
            .where(Dataset.space_id == space_id, Dataset.kind == kind)
            .order_by(Dataset.created_at.desc(), Dataset.id.desc())
        )
    )


def _field_views(kind: str, headers: list[str], saved: dict) -> list[dict[str, object]]:
    fields = FIELDS[kind]
    guesses = guess_mapping(fields, headers)
    views = []
    for field in fields:
        if field.key in saved:
            selected = saved[field.key] if saved[field.key] in headers else ""
        else:
            selected = guesses[field.key]
        views.append(
            {
                "key": field.key,
                "label": field.label,
                "required": field.required,
                "selected": selected,
            }
        )
    return views


def _saved_map(dataset: Dataset) -> dict:
    if not dataset.column_map:
        return {}
    try:
        loaded = json.loads(dataset.column_map)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _store_table(
    request: Request,
    db: Session,
    space: Space,
    user: User,
    kind: str,
    filename: str,
    content: bytes,
    section_slug: str,
    owner_name: str = "",
    replace_same_owner: bool = False,
):
    owner_name = " ".join(owner_name.split())[:255]
    if replace_same_owner and owner_name:
        key = person_key(owner_name)
        for dataset in _datasets(db, space.id, kind):
            if person_key(dataset.owner_name or "") == key:
                remove_dataset(db, request.app.state.settings.data_dir, dataset)
    relative = save_bytes(request.app.state.settings.data_dir, space.id, filename, content)
    dataset = Dataset(
        space_id=space.id,
        kind=kind,
        filename=filename,
        stored_path=relative,
        status="mapping",
        column_map=None,
        row_count=0,
        uploaded_by=user.id,
        owner_name=owner_name,
        created_at=utcnow(),
    )
    db.add(dataset)
    db.commit()
    return redirect(request, f"/s/{space.id}/datasets/{dataset.id}/columns")


async def _upload(
    request: Request,
    db: Session,
    space: Space,
    user: User,
    kind: str,
    file: UploadFile,
    section_slug: str,
    owner_name: str = "",
    replace_same_owner: bool = False,
):
    try:
        filename, content = await read_upload(file, TABLE_SUFFIXES, "Нужен файл CSV или Excel (.csv, .xlsx).")
    except ValueError as exc:
        return redirect(request, f"/s/{space.id}/{section_slug}", str(exc), kind="error")
    return _store_table(
        request,
        db,
        space,
        user,
        kind,
        filename,
        content,
        section_slug,
        owner_name=owner_name,
        replace_same_owner=replace_same_owner,
    )


def _cluster_branches(request: Request, dataset: Dataset, groups: list[dict]) -> list[dict] | None:
    try:
        path = resolve_path(request.app.state.settings.data_dir, dataset.stored_path)
        headers, records = read_table(path)
        return build_branches(groups, analyze_autocluster(headers, records))
    except (OSError, ValueError):
        return None


def _cluster_scheme(request: Request, dataset: Dataset, groups: list[dict], root_name: str) -> dict:
    branches = _cluster_branches(request, dataset, groups)
    if branches:
        return layout_branches(root_name, branches)
    return layout_cluster_tree(groups, root_name)


def _owner_names(datasets: list[Dataset]) -> list[str]:
    seen: dict[str, str] = {}
    for dataset in datasets:
        name = " ".join((dataset.owner_name or "").split())
        if name:
            seen.setdefault(person_key(name), name)
    return sorted(seen.values(), key=person_key)


def _cluster_people(db: Session, datasets: list[Dataset]) -> list[str]:
    if not datasets:
        return []
    ids = [dataset.id for dataset in datasets]
    rows = db.scalars(select(ClusterMember.name).where(ClusterMember.dataset_id.in_(ids)))
    seen: dict[str, str] = {}
    for name in rows:
        clean = " ".join((name or "").split())
        if clean:
            seen.setdefault(person_key(clean), clean)
    return sorted(seen.values(), key=person_key)


def _cluster_sort(label: str):
    if label.isdigit():
        return (0, int(label), "")
    return (1, 0, label.casefold())


def _cluster_groups(db: Session, datasets: list[Dataset]) -> dict[int, list[dict[str, object]]]:
    if not datasets:
        return {}
    ids = [dataset.id for dataset in datasets]
    members = list(db.scalars(select(ClusterMember).where(ClusterMember.dataset_id.in_(ids))))
    buckets: dict[int, dict[str, list[ClusterMember]]] = {}
    for member in members:
        buckets.setdefault(member.dataset_id, {}).setdefault(member.cluster_label, []).append(member)
    groups: dict[int, list[dict[str, object]]] = {}
    for dataset in datasets:
        labels = buckets.get(dataset.id, {})
        ordered = []
        for label in sorted(labels, key=_cluster_sort):
            people = sorted(labels[label], key=lambda item: (-(item.shared_cm if item.shared_cm is not None else -1), item.name))
            ordered.append({"label": label, "members": people})
        groups[dataset.id] = ordered
    return groups


@router.get("/s/{space_id}/dna")
def dna_page(
    request: Request,
    page: int = 1,
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    total = db.scalar(select(func.count()).select_from(DnaMatch).where(DnaMatch.space_id == space.id)) or 0
    current, pages, offset = paginate(page, 100, total)
    matches = list(
        db.scalars(
            select(DnaMatch)
            .where(DnaMatch.space_id == space.id)
            .order_by(DnaMatch.shared_cm.is_(None), DnaMatch.shared_cm.desc(), DnaMatch.name)
            .offset(offset)
            .limit(100)
        )
    )
    max_cm = db.scalar(select(func.max(DnaMatch.shared_cm)).where(DnaMatch.space_id == space.id))
    return render(
        request,
        "dna.html",
        user=user,
        space=space,
        section="dna",
        datasets=_datasets(db, space.id, KIND_MATCHES),
        matches=matches,
        total=total,
        max_cm=max_cm,
        pager=pager(f"/s/{space.id}/dna", current, pages),
    )


@router.post("/s/{space_id}/dna")
async def upload_dna(
    request: Request,
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    file: UploadFile = File(),
):
    return await _upload(request, db, space, user, KIND_MATCHES, file, "dna")


@router.get("/s/{space_id}/clusters")
def clusters_page(
    request: Request,
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    datasets = _datasets(db, space.id, KIND_CLUSTERS)
    groups = _cluster_groups(db, datasets)
    owners = _owner_names(datasets)
    self_name = " ".join((space.cluster_self or "").split())
    self_known = bool(self_name) and person_key(self_name) in {person_key(name) for name in owners}
    trees = {}
    self_scheme = None
    separate_blocks = []
    if self_known:
        items = []
        for dataset in datasets:
            grouped = groups.get(dataset.id, [])
            if dataset.status != "ready" or not grouped:
                continue
            items.append(
                {
                    "owner": dataset.owner_name or dataset.filename,
                    "names": [member.name for group in grouped for member in group["members"]],
                    "branches": _cluster_branches(request, dataset, grouped),
                    "groups": grouped,
                    "dataset": dataset,
                }
            )
        connected, separate = split_for_self(items, self_name)
        if connected:
            self_scheme = layout_self_sides(self_name, connected)
        for item in separate:
            separate_blocks.append(
                {
                    "owner": item["owner"],
                    "scheme": _cluster_scheme(request, item["dataset"], item["groups"], item["owner"]),
                }
            )
    else:
        for dataset in datasets:
            grouped = groups.get(dataset.id, [])
            if dataset.status != "ready" or not grouped:
                continue
            trees[dataset.id] = _cluster_scheme(request, dataset, grouped, space.name)
    matrix_ids = set(
        db.scalars(
            select(ClusterPair.dataset_id).where(ClusterPair.space_id == space.id).distinct()
        )
    )
    return render(
        request,
        "clusters.html",
        user=user,
        space=space,
        section="clusters",
        datasets=datasets,
        cluster_groups=groups,
        trees=trees,
        owners=owners,
        cluster_people=_cluster_people(db, datasets),
        self_name=self_name,
        self_known=self_known,
        self_scheme=self_scheme,
        separate_blocks=separate_blocks,
        matrix_ids=matrix_ids,
    )


def _is_autocluster(dataset: Dataset) -> bool:
    return bool(dataset.column_map) and '"source": "autocluster"' in dataset.column_map


def _save_autocluster(db: Session, dataset: Dataset, parsed: AutoCluster) -> None:
    db.execute(delete(ClusterPair).where(ClusterPair.dataset_id == dataset.id))
    db.execute(delete(ClusterMember).where(ClusterMember.dataset_id == dataset.id))
    members: list[ClusterMember] = []
    for person in parsed.people:
        member = ClusterMember(
            dataset_id=dataset.id,
            space_id=dataset.space_id,
            cluster_label=person.cluster[:80],
            name=person.name[:500],
            shared_cm=person.shared_cm,
            kit=person.kit[:64],
            segments=person.segments,
            largest_cm=person.largest_cm,
            icw=person.icw,
            relationship=person.relationship[:120],
            tree_url=person.tree_url[:1000],
            tree_count=person.tree_count,
            note=person.note[:500],
        )
        db.add(member)
        members.append(member)
    db.flush()
    for left, right in parsed.pairs:
        low = members[left]
        high = members[right]
        if low.id > high.id:
            low, high = high, low
        db.add(
            ClusterPair(
                dataset_id=dataset.id,
                space_id=dataset.space_id,
                member_low_id=low.id,
                member_high_id=high.id,
            )
        )
    dataset.column_map = json.dumps({"source": "autocluster"}, ensure_ascii=False)
    dataset.status = "ready"
    dataset.row_count = len(members)


def _cluster_hue(label: str) -> str:
    if label.isdigit():
        return f"ac-h{(int(label) - 1) % 24 + 1}"
    return "ac-h1"


def _matrix_rows(db: Session, dataset: Dataset) -> tuple[list[dict], int]:
    members = list(
        db.scalars(select(ClusterMember).where(ClusterMember.dataset_id == dataset.id))
    )
    members.sort(key=lambda item: (_cluster_sort(item.cluster_label), item.id))
    pairs = list(db.scalars(select(ClusterPair).where(ClusterPair.dataset_id == dataset.id)))
    by_ends = {(pair.member_low_id, pair.member_high_id): pair.id for pair in pairs}
    rows = []
    previous = None
    for member in members:
        cells = []
        for other in members:
            low, high = sorted((member.id, other.id))
            pair_id = by_ends.get((low, high)) if member.id != other.id else None
            same = member.cluster_label == other.cluster_label
            css = _cluster_hue(member.cluster_label) if same else "ac-cross"
            if member.id == other.id:
                cells.append({"css": css, "href": ""})
            elif pair_id:
                cells.append(
                    {
                        "css": css if same else "ac-cross",
                        "href": f"/s/{dataset.space_id}/clusters/{dataset.id}/pairs/{pair_id}",
                    }
                )
            else:
                cells.append({"css": "", "href": ""})
        rows.append(
            {
                "id": member.id,
                "name": member.name,
                "cluster_label": member.cluster_label,
                "hue": _cluster_hue(member.cluster_label),
                "group_start": previous is not None and previous != member.cluster_label,
                "cells": cells,
            }
        )
        previous = member.cluster_label
    return rows, len(pairs)


@router.post("/s/{space_id}/clusters")
async def upload_clusters(
    request: Request,
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    file: UploadFile = File(),
    owner: str = Form(""),
    owner_other: str = Form(""),
):
    chosen = " ".join(owner.split()) or " ".join(owner_other.split())
    try:
        filename, content = await read_upload(
            file,
            CLUSTER_SUFFIXES,
            "Нужен файл CSV, Excel или HTML AutoClusters.",
        )
    except ValueError as exc:
        return redirect(request, f"/s/{space.id}/clusters", str(exc), kind="error")
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    try:
        parsed = parse_autocluster(filename, content)
    except ValueError as exc:
        return redirect(request, f"/s/{space.id}/clusters", str(exc), kind="error")
    if parsed is None:
        if suffix in {".html", ".htm"}:
            return redirect(request, f"/s/{space.id}/clusters", "В файле нет матрицы AutoClusters.", kind="error")
        return _store_table(
            request,
            db,
            space,
            user,
            KIND_CLUSTERS,
            filename,
            content,
            "clusters",
            owner_name=chosen,
            replace_same_owner=True,
        )
    if chosen:
        key = person_key(chosen)
        for dataset in _datasets(db, space.id, KIND_CLUSTERS):
            if person_key(dataset.owner_name or "") == key:
                remove_dataset(db, request.app.state.settings.data_dir, dataset)
    relative = save_bytes(request.app.state.settings.data_dir, space.id, filename, content)
    dataset = Dataset(
        space_id=space.id,
        kind=KIND_CLUSTERS,
        filename=filename,
        stored_path=relative,
        status="mapping",
        column_map=None,
        row_count=0,
        uploaded_by=user.id,
        owner_name=chosen[:255],
        created_at=utcnow(),
    )
    db.add(dataset)
    db.flush()
    _save_autocluster(db, dataset, parsed)
    db.commit()
    return redirect(
        request,
        f"/s/{space.id}/clusters/{dataset.id}/matrix",
        f"Загружено кластеров: {_cluster_count(parsed)}. Совпадений: {len(parsed.pairs)}.",
    )


def _cluster_count(parsed: AutoCluster) -> int:
    return len({person.cluster for person in parsed.people})


@router.get("/s/{space_id}/clusters/{dataset_id}/matrix")
def cluster_matrix(
    request: Request,
    dataset_id: int,
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    dataset = get_dataset(db, space.id, dataset_id)
    if dataset.kind != KIND_CLUSTERS or dataset.status != "ready":
        raise HTTPException(status_code=404, detail="Таблица не найдена.")
    rows, pair_count = _matrix_rows(db, dataset)
    if not rows:
        raise HTTPException(status_code=404, detail="Таблица не найдена.")
    return render(
        request,
        "cluster_matrix.html",
        user=user,
        space=space,
        section="clusters",
        dataset=dataset,
        rows=rows,
        pair_count=pair_count,
        cluster_count=len({row["cluster_label"] for row in rows}),
    )


@router.get("/s/{space_id}/clusters/{dataset_id}/pairs/{pair_id}")
def cluster_pair(
    request: Request,
    dataset_id: int,
    pair_id: int,
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    dataset = get_dataset(db, space.id, dataset_id)
    pair = db.get(ClusterPair, pair_id)
    if (
        dataset.kind != KIND_CLUSTERS
        or pair is None
        or pair.dataset_id != dataset.id
        or pair.space_id != space.id
    ):
        raise HTTPException(status_code=404, detail="Совпадение не найдено.")
    left = db.get(ClusterMember, pair.member_low_id)
    right = db.get(ClusterMember, pair.member_high_id)
    if left is None or right is None:
        raise HTTPException(status_code=404, detail="Совпадение не найдено.")
    return render(
        request,
        "cluster_pair.html",
        user=user,
        space=space,
        section="clusters",
        dataset=dataset,
        people=(left, right),
    )


@router.post("/s/{space_id}/clusters/self")
def set_cluster_self(
    request: Request,
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    name: str = Form(""),
):
    key = person_key(name)
    chosen = None
    for owner_name in _owner_names(_datasets(db, space.id, KIND_CLUSTERS)):
        if person_key(owner_name) == key:
            chosen = owner_name
            break
    if not chosen:
        return redirect(request, f"/s/{space.id}/clusters", "Сначала укажите этого человека в «Чьи кластеры».", kind="error")
    space.cluster_self = chosen[:500]
    db.commit()
    return redirect(request, f"/s/{space.id}/clusters", "Вы указаны в схеме кластеров.")


def _people_page(db, data_dir, space_id: int, query: str, page: int, status: str):
    rows = assembled_people(db, data_dir, space_id, query)
    if rows is None:
        return None, None, 0, 0, 0
    total = len(rows)
    current, pages, offset = paginate(page, PEOPLE_PAGE, total)
    chunk = rows[offset : offset + PEOPLE_PAGE]
    start = offset + 1 if chunk else 0
    end = offset + len(chunk)
    nav = pager(
        f"/s/{space_id}/links",
        current,
        pages,
        page_key="people_page",
        status=status,
        q=query,
    )
    return chunk, nav, total, start, end


@router.get("/s/{space_id}/links")
def links_page(
    request: Request,
    status: str = "proposed",
    page: int = 1,
    people_page: int = 1,
    q: str = "",
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if status not in LINK_STATUSES:
        status = "proposed"
    datasets = _datasets(db, space.id, KIND_LINKS)
    reports = {dataset.id: dataset_link_report(db, dataset) for dataset in datasets if dataset.status == "ready"}
    total = (
        db.scalar(
            select(func.count())
            .select_from(PersonLink)
            .where(PersonLink.space_id == space.id, PersonLink.status == status)
        )
        or 0
    )
    current, pages, offset = paginate(page, 50, total)
    person_a = aliased(Person)
    person_b = aliased(Person)
    tree_a = aliased(Tree)
    tree_b = aliased(Tree)
    rows = db.execute(
        select(PersonLink, person_a, person_b, tree_a, tree_b)
        .join(person_a, person_a.id == PersonLink.person_low_id)
        .join(person_b, person_b.id == PersonLink.person_high_id)
        .join(tree_a, tree_a.id == person_a.tree_id)
        .join(tree_b, tree_b.id == person_b.tree_id)
        .where(PersonLink.space_id == space.id, PersonLink.status == status)
        .order_by(PersonLink.id.desc())
        .offset(offset)
        .limit(50)
    ).all()
    pairs = [
        {"id": link.id, "status": link.status, "low": left, "high": right, "low_tree": left_tree, "high_tree": right_tree}
        for link, left, right, left_tree, right_tree in rows
    ]
    people_rows, people_nav, people_total, people_start, people_end = _people_page(
        db, request.app.state.settings.data_dir, space.id, q, people_page, status
    )
    return render(
        request,
        "links.html",
        user=user,
        space=space,
        section="links",
        datasets=datasets,
        link_stats=reports,
        pairs=pairs,
        status_filter=status,
        empty_links=EMPTY_LINKS[status],
        pager=pager(f"/s/{space.id}/links", current, pages, status=status, q=q),
        tree_query=q,
        assembled=people_rows,
        people_pager=people_nav,
        people_total=people_total,
        people_start=people_start,
        people_end=people_end,
    )


@router.post("/s/{space_id}/links")
async def upload_links(
    request: Request,
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    file: UploadFile = File(),
):
    return await _upload(request, db, space, user, KIND_LINKS, file, "links")


def _save_geneteka(request: Request, db: Session, space: Space, user: User, loaded):
    try:
        message, old = store_geneteka(db, request.app.state.settings.data_dir, space, user, loaded)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return None, str(exc)
    except Exception:
        db.rollback()
        raise
    if old:
        remove_file(request.app.state.settings.data_dir, old)
    return message, None


@router.post("/s/{space_id}/links/geneteka")
async def import_geneteka(
    request: Request,
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if "application/json" in request.headers.get("content-type", ""):
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Некорректный индекс.") from None
        try:
            loaded = load_from_payload(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        message, error = _save_geneteka(request, db, space, user, loaded)
        if error:
            raise HTTPException(status_code=400, detail=error)
        return {"message": message}
    form = await request.form()
    url = str(form.get("url") or "").strip()
    try:
        loaded = fetch_parish(url)
    except ValueError as exc:
        return redirect(request, f"/s/{space.id}/links", str(exc), kind="error")
    except GenetekaError:
        return redirect(
            request,
            f"/s/{space.id}/links",
            f"Не удалось открыть Geneteka. {local_hint(space.id)}",
            kind="error",
        )
    message, error = _save_geneteka(request, db, space, user, loaded)
    if error:
        return redirect(request, f"/s/{space.id}/links", error, kind="error")
    return redirect(request, f"/s/{space.id}/links", message)


def _columns_response(
    request: Request,
    db: Session,
    space: Space,
    user: User,
    dataset: Dataset,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    headers: list[str] = []
    preview: list[dict[str, str]] = []
    fields: list[dict[str, object]] = []
    try:
        path = resolve_path(request.app.state.settings.data_dir, dataset.stored_path)
        headers, rows = read_table(path)
        preview = rows[:5]
        fields = _field_views(dataset.kind, headers, _saved_map(dataset))
    except FileNotFoundError:
        error = error or "Файл не найден."
    except ValueError as exc:
        error = error or str(exc)
    slug, label = BACK[dataset.kind]
    return render(
        request,
        "columns.html",
        status_code=status_code if error is None else max(status_code, 400),
        user=user,
        space=space,
        section={"matches": "dna", "clusters": "clusters", "links": "links"}[dataset.kind],
        dataset=dataset,
        headers=headers,
        preview=preview,
        fields=fields,
        back_slug=slug,
        back_label=label,
        error=error,
    )


@router.get("/s/{space_id}/datasets/{dataset_id}/columns")
def columns_page(
    request: Request,
    dataset_id: int,
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    dataset = get_dataset(db, space.id, dataset_id)
    if _is_autocluster(dataset):
        return redirect(request, f"/s/{space.id}/clusters/{dataset.id}/matrix")
    return _columns_response(request, db, space, user, dataset)


@router.post("/s/{space_id}/datasets/{dataset_id}/columns")
async def save_columns(
    request: Request,
    dataset_id: int,
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    dataset = get_dataset(db, space.id, dataset_id)
    if _is_autocluster(dataset):
        return redirect(request, f"/s/{space.id}/clusters/{dataset.id}/matrix")
    form = await request.form()
    summary = None
    try:
        path = resolve_path(request.app.state.settings.data_dir, dataset.stored_path)
        headers, rows = read_table(path)
        mapping = parse_mapping(form, FIELDS[dataset.kind], headers)
        replace_imported(db, dataset, mapping, rows)
        if dataset.kind == KIND_LINKS:
            summary = match_dataset(db, dataset)
        db.commit()
    except (ValueError, FileNotFoundError) as exc:
        db.rollback()
        message = "Файл не найден." if isinstance(exc, FileNotFoundError) else str(exc)
        return _columns_response(request, db, space, user, dataset, error=message, status_code=400)
    message = "Столбцы сохранены."
    if summary:
        message = f"Столбцы сохранены. {summary}"
    return redirect(request, f"/s/{space.id}/{BACK[dataset.kind][0]}", message)


@router.post("/s/{space_id}/datasets/{dataset_id}/delete")
def delete_dataset(
    request: Request,
    dataset_id: int,
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    dataset = get_dataset(db, space.id, dataset_id)
    kind = dataset.kind
    remove_dataset(db, request.app.state.settings.data_dir, dataset)
    return redirect(request, f"/s/{space.id}/{BACK[kind][0]}", "Таблица удалена.")


@router.get("/s/{space_id}/files/datasets/{dataset_id}")
def download_dataset(
    request: Request,
    dataset_id: int,
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    dataset = get_dataset(db, space.id, dataset_id)
    try:
        path = resolve_path(request.app.state.settings.data_dir, dataset.stored_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Файл не найден.") from None
    return FileResponse(path, filename=dataset.filename, content_disposition_type="attachment")


@router.post("/s/{space_id}/links/trees")
def assemble_trees(
    request: Request,
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    summary = assemble_link_trees(db, request.app.state.settings.data_dir, space.id)
    db.commit()
    return redirect(request, f"/s/{space.id}/links", summary)


@router.post("/s/{space_id}/links/{dataset_id}/match")
def run_match(
    request: Request,
    dataset_id: int,
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    dataset = get_dataset(db, space.id, dataset_id)
    if dataset.kind != KIND_LINKS or dataset.status != "ready":
        raise HTTPException(status_code=404, detail="Таблица не найдена.")
    summary = match_dataset(db, dataset)
    db.commit()
    return redirect(request, f"/s/{space.id}/links", f"Поиск завершён. {summary}")


@router.post("/s/{space_id}/links/{link_id}/status")
def set_link_status(
    request: Request,
    link_id: int,
    status: str = Form(""),
    back: str = Form("proposed"),
    space: Space = Depends(require_space),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if status not in LINK_STATUSES:
        raise HTTPException(status_code=400, detail="Неизвестный статус.")
    link = get_link(db, space.id, link_id)
    link.status = status
    db.commit()
    target = back if back in LINK_STATUSES else "proposed"
    return redirect(request, f"/s/{space.id}/links?status={target}", STATUS_FLASH[status])
