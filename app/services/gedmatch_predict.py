from __future__ import annotations

import math
import re
from collections import defaultdict
from decimal import Decimal
from urllib.parse import quote

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.importers.names import normalize_name
from app.models import (
    ClusterMember,
    Dataset,
    GedMatchHit,
    GedMatchKit,
    GedMatchMatrix,
    GedMatchMatrixKit,
    Person,
    Tree,
)
from app.services.cluster_self import person_key

# Shared cM Project ranges overlap, so one match can fit several relationships.
# Clusters join kits when the stronger direction reaches this floor:
# the low end of «двоюродный во втором колене» below.
RANGES = (
    ("родитель или ребёнок", 2376, 3720),
    ("брат или сестра", 1613, 3488),
    ("дед, бабка, внук или внучка, дядя, тётя, племянник или полусиблинг", 1156, 2436),
    ("двоюродный, прадед или двоюродный дядя", 396, 1397),
    ("двоюродный во втором колене", 46, 515),
    ("троюродный или дальше", 0, 234),
)


STRONG_CM = 46
SPLIT_AT = 8
WIDE_AT = 12
CLOSE_GEN = 3.1
TIGHT_GEN = 2.1


def relationship_labels(total_cm: float | None) -> list[str]:
    if total_cm is None:
        return []
    return [label for label, low, high in RANGES if low <= total_cm <= high]


def owner_parts(name: str) -> tuple[str, str]:
    parts = name.split()
    if len(parts) >= 2:
        return " ".join(parts[:-1]), parts[-1]
    return "", name


def save_kit_owner(db: Session, space_id: int, source_kit: str, owner: dict) -> None:
    source = source_kit.strip().upper()
    row = db.scalar(
        select(GedMatchKit).where(GedMatchKit.space_id == space_id, GedMatchKit.source_kit == source)
    )
    if row is None:
        row = GedMatchKit(space_id=space_id, source_kit=source)
        db.add(row)
    row.name = str(owner.get("name") or "")[:500]
    row.email = str(owner.get("email") or "")[:255]
    row.company = str(owner.get("company") or "")[:80]


def replace_hits(
    db: Session, space_id: int, source_kit: str, rows: list[dict], owner: dict | None = None
) -> int:
    source = source_kit.strip().upper()
    kept = {
        hit.match_kit: hit.person_id
        for hit in db.scalars(
            select(GedMatchHit).where(GedMatchHit.space_id == space_id, GedMatchHit.source_kit == source)
        )
    }
    db.execute(delete(GedMatchHit).where(GedMatchHit.space_id == space_id, GedMatchHit.source_kit == source))
    stored = 0
    for row in rows:
        kit = str(row.get("kit") or "").strip().upper()
        if not kit or kit == source:
            continue
        db.add(
            GedMatchHit(
                space_id=space_id,
                source_kit=source,
                match_kit=kit,
                name=str(row.get("name") or "")[:500],
                name_key=normalize_name(str(row.get("name") or "")),
                total_cm=row.get("total_cm"),
                largest_cm=row.get("largest_cm"),
                overlap=row.get("overlap"),
                generations=row.get("generations"),
                date_compared=str(row.get("date_compared") or "")[:32],
                company=str(row.get("company") or "")[:80],
                person_id=kept.get(kit),
            )
        )
        stored += 1
    if owner:
        save_kit_owner(db, space_id, source, owner)
    db.commit()
    return stored


def gen_tier_class(generations: float | None) -> str | None:
    if generations is None:
        return None
    if generations < 1.1:
        tier = 1
    else:
        tier = int(Decimal(str(generations)) - Decimal("0.1")) + 1
    if tier >= 9:
        return "gedmatch-gen-9"
    return f"gedmatch-gen-{tier}"


def _kit_display_name(header_name: str, fallback: str) -> str:
    name = header_name.strip() or fallback.strip()
    if "@" in name:
        return ""
    return name


SEEDED_MATRIX = "Ruslan Aharodnik"


def list_matrices(db: Session, space_id: int) -> list[GedMatchMatrix]:
    return list(
        db.scalars(
            select(GedMatchMatrix)
            .where(GedMatchMatrix.space_id == space_id)
            .order_by(GedMatchMatrix.id)
        )
    )


def choose_matrix(matrices: list[GedMatchMatrix], matrix_id: str) -> GedMatchMatrix | None:
    wanted = matrix_id.strip()
    if wanted:
        for item in matrices:
            if str(item.id) == wanted:
                return item
    for item in matrices:
        if item.name == SEEDED_MATRIX:
            return item
    for item in matrices:
        if item.receives_unlisted:
            return item
    return matrices[0] if matrices else None


def assign_matrix_kit(db: Session, space_id: int, name: str, kit: str) -> None:
    """Create a named matrix if missing and attach one source kit. Does not write hits."""
    cleaned = " ".join(name.split())[:200]
    if not cleaned:
        return
    row = db.scalar(
        select(GedMatchMatrix).where(
            GedMatchMatrix.space_id == space_id,
            GedMatchMatrix.name == cleaned,
        )
    )
    if row is None:
        row = GedMatchMatrix(space_id=space_id, name=cleaned, receives_unlisted=False)
        db.add(row)
        db.flush()
    code = kit.strip().upper()
    if code and db.get(GedMatchMatrixKit, (row.id, code)) is None:
        db.add(GedMatchMatrixKit(matrix_id=row.id, kit=code))
    db.commit()


def kits_for_matrix(db: Session, matrix: GedMatchMatrix) -> list[str]:
    kits = set(
        db.scalars(select(GedMatchMatrixKit.kit).where(GedMatchMatrixKit.matrix_id == matrix.id))
    )
    if matrix.receives_unlisted:
        sources = set(
            db.scalars(
                select(GedMatchHit.source_kit)
                .where(GedMatchHit.space_id == matrix.space_id)
                .distinct()
            )
        )
        listed = set(
            db.scalars(
                select(GedMatchMatrixKit.kit)
                .join(GedMatchMatrix, GedMatchMatrix.id == GedMatchMatrixKit.matrix_id)
                .where(GedMatchMatrix.space_id == matrix.space_id)
            )
        )
        kits |= sources - listed
    return sorted(kits)


def correspondence_matrix(db: Session, space_id: int, only_kits: list[str] | None = None) -> dict:
    owners = list(
        db.scalars(
            select(GedMatchKit)
            .where(GedMatchKit.space_id == space_id)
            .order_by(GedMatchKit.source_kit)
        )
    )
    header_names = {row.source_kit: row.name or "" for row in owners}
    if only_kits is not None:
        kits = list(only_kits)
    else:
        kits = [row.source_kit for row in owners]
    if not kits and only_kits is None:
        kits = list(
            db.scalars(
                select(GedMatchHit.source_kit)
                .where(GedMatchHit.space_id == space_id)
                .distinct()
                .order_by(GedMatchHit.source_kit)
            )
        )
        header_names = {kit: header_names.get(kit, "") for kit in kits}
    wanted = set(kits)
    cells = {kit: {other: None for other in kits} for kit in kits}
    if not kits:
        return {"kits": [], "rows": []}
    hits = db.scalars(
        select(GedMatchHit).where(
            GedMatchHit.space_id == space_id,
            GedMatchHit.source_kit.in_(wanted),
            GedMatchHit.match_kit.in_(wanted),
        )
    )
    for hit in hits:
        cells[hit.source_kit][hit.match_kit] = {
            "cm": hit.total_cm,
            "tier": gen_tier_class(hit.generations),
        }
    missing = [kit for kit in kits if not (header_names.get(kit) or "").strip()]
    fallback_names: dict[str, str] = {}
    if missing:
        named = db.scalars(
            select(GedMatchHit)
            .where(
                GedMatchHit.space_id == space_id,
                GedMatchHit.match_kit.in_(missing),
                GedMatchHit.name != "",
            )
            .order_by(GedMatchHit.id)
        )
        for hit in named:
            if hit.match_kit not in fallback_names and (hit.name or "").strip():
                fallback_names[hit.match_kit] = hit.name.strip()
    return {
        "kits": kits,
        "rows": [
            {
                "kit": kit,
                "name": _kit_display_name(header_names.get(kit, ""), fallback_names.get(kit, "")),
                "cells": [cells[kit][other] for other in kits],
            }
            for kit in kits
        ],
    }


def header_names_for(db: Session, space_id: int, kits: list[str]) -> dict[str, str]:
    """Name shown beside each kit on the matrix. Blank when that kit has none."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for kit in kits:
        code = kit.strip().upper()
        if code and code not in seen:
            seen.add(code)
            cleaned.append(code)
    if not cleaned:
        return {}
    return {row["kit"]: row["name"] for row in correspondence_matrix(db, space_id, cleaned)["rows"]}


def strong_clusters(kits: list[str], directed: dict[tuple[str, str], float | None]) -> list[list[str]]:
    """Groups of scraped kits that share a strong match with each other.

    An edge exists when the stronger stored direction is at least STRONG_CM.
    A missing direction does not invent a value. Clusters are the connected
    components of that graph, largest first, kits in code order.
    """
    wanted = set(kits)
    best: dict[tuple[str, str], float] = {}
    for (source, match), cm in directed.items():
        if cm is None or source not in wanted or match not in wanted or source == match:
            continue
        key = (source, match) if source < match else (match, source)
        best[key] = max(best.get(key, 0.0), cm)
    parent = {kit: kit for kit in kits}

    def find(kit: str) -> str:
        while parent[kit] != kit:
            parent[kit] = parent[parent[kit]]
            kit = parent[kit]
        return kit

    for (left, right), cm in best.items():
        if cm >= STRONG_CM:
            parent[find(left)] = find(right)
    groups: dict[str, list[str]] = {}
    for kit in kits:
        groups.setdefault(find(kit), []).append(kit)
    clusters = [sorted(group) for group in groups.values()]
    clusters.sort(key=lambda group: (-len(group), group[0]))
    return clusters


def _tie_key(generations: float | None, total_cm: float | None) -> tuple[float, float]:
    """Smaller means a closer tie: lower Gen, then higher Total cM."""
    gen_rank = generations if generations is not None else math.inf
    cm_rank = -(total_cm if total_cm is not None else -math.inf)
    return (gen_rank, cm_rank)


def _pair(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left < right else (right, left)


def _undirected_edges(
    kits: set[str],
    directed: dict[tuple[str, str], float | None],
    generations: dict[tuple[str, str], float | None],
) -> dict[tuple[str, str], tuple[float, float]]:
    best: dict[tuple[str, str], tuple[float, float]] = {}
    for (source, match), cm in directed.items():
        if source not in kits or match not in kits or source == match:
            continue
        key = _pair(source, match)
        strength = _tie_key(generations.get((source, match)), cm)
        if key not in best or strength < best[key]:
            best[key] = strength
    return best


def _components(kits: list[str], edges: set[tuple[str, str]]) -> list[list[str]]:
    parent = {kit: kit for kit in kits}

    def find(kit: str) -> str:
        while parent[kit] != kit:
            parent[kit] = parent[parent[kit]]
            kit = parent[kit]
        return kit

    for left, right in edges:
        parent[find(left)] = find(right)
    groups: dict[str, list[str]] = {}
    for kit in kits:
        groups.setdefault(find(kit), []).append(kit)
    return [sorted(group) for group in groups.values()]


def _order_subgroups(
    groups: list[list[str]],
    edges: dict[tuple[str, str], tuple[float, float]],
) -> list[list[str]]:
    """Keep more tightly linked subgroups next to each other."""
    groups = sorted(groups, key=lambda group: group[0])
    if len(groups) <= 1:
        return groups
    members = {index: set(group) for index, group in enumerate(groups)}

    def link(left: int, right: int) -> tuple[float, float]:
        best: tuple[float, float] | None = None
        for kit in members[left]:
            for other in members[right]:
                strength = edges.get(_pair(kit, other))
                if strength is not None and (best is None or strength < best):
                    best = strength
        return best if best is not None else (math.inf, math.inf)

    seed = min(
        ((link(i, j), i, j) for i in range(len(groups)) for j in range(i + 1, len(groups))),
    )
    left, right = seed[1], seed[2]
    chain = [left, right] if groups[left][0] <= groups[right][0] else [right, left]
    unused = set(range(len(groups))) - set(chain)
    while unused:
        end_left, end_right = chain[0], chain[-1]
        attach = min(
            (
                (link(end, index), side, groups[index][0], index)
                for index in unused
                for side, end in ((0, end_left), (1, end_right))
            ),
        )
        if attach[0] == (math.inf, math.inf):
            chain.extend(sorted(unused, key=lambda index: groups[index][0]))
            break
        index = attach[3]
        if attach[1] == 0:
            chain.insert(0, index)
        else:
            chain.append(index)
        unused.remove(index)
    return [groups[index] for index in chain]


def _absorb_singletons(
    kits: list[str],
    parts: list[list[str]],
    edges: dict[tuple[str, str], tuple[float, float]],
) -> list[list[str]]:
    """Singletons rejoin the neighbor with the strongest stored edge."""
    if not any(len(part) == 1 for part in parts):
        return parts
    parent = {kit: kit for kit in kits}

    def find(kit: str) -> str:
        while parent[kit] != kit:
            parent[kit] = parent[parent[kit]]
            kit = parent[kit]
        return kit

    for part in parts:
        if len(part) > 1:
            root = part[0]
            for kit in part[1:]:
                parent[find(kit)] = find(root)
    for kit in sorted(kit for part in parts if len(part) == 1 for kit in part):
        best: tuple[tuple[float, float], str] | None = None
        for other in kits:
            if other == kit:
                continue
            strength = edges.get(_pair(kit, other))
            if strength is None:
                continue
            candidate = (strength, other)
            if best is None or candidate < best:
                best = candidate
        if best is not None:
            parent[find(kit)] = find(best[1])
    regrouped: dict[str, list[str]] = {}
    for kit in kits:
        regrouped.setdefault(find(kit), []).append(kit)
    return [sorted(group) for group in regrouped.values()]


def _split_component(
    kits: list[str],
    edges: dict[tuple[str, str], tuple[float, float]],
) -> list[list[str]]:
    """Keep cutting one component while it is large and a weak edge remains."""
    if len(kits) < SPLIT_AT:
        return [kits]
    kitset = set(kits)
    local = {
        key: strength
        for key, strength in edges.items()
        if key[0] in kitset and key[1] in kitset
    }
    floor = TIGHT_GEN if len(kits) >= WIDE_AT else CLOSE_GEN
    active = set(local)
    for key, strength in sorted(local.items(), key=lambda item: (item[1], item[0]), reverse=True):
        if strength[0] < floor:
            break
        active.discard(key)
        parts = _components(kits, active)
        if len(parts) <= 1:
            continue
        parts = _absorb_singletons(kits, parts, local)
        if len(parts) <= 1:
            continue
        pieces: list[list[str]] = []
        for part in _order_subgroups(parts, local):
            pieces.extend(_split_component(part, local))
        return pieces
    return [kits]


def split_loose_clusters(
    clusters: list[list[str]],
    directed: dict[tuple[str, str], float | None],
    generations: dict[tuple[str, str], float | None],
) -> list[list[str]]:
    """Split a large cluster into smaller, more tightly linked pieces.

    Keep cutting while a component has 8 or more kits and a weak edge remains:
    Gen 3.1 or higher, or, once the component has 12 or more kits, the weakest
    edge that is not close (Gen under 2.1). Singletons rejoin their strongest
    neighbor. A group that stays one piece is left intact.
    """
    split: list[list[str]] = []
    for cluster in clusters:
        if len(cluster) < SPLIT_AT:
            split.append(cluster)
            continue
        edges = _undirected_edges(set(cluster), directed, generations)
        split.extend(_split_component(cluster, edges))
    return split


def _cluster_hue(label: str) -> str:
    if label.isdigit():
        return f"ac-h{(int(label) - 1) % 24 + 1}"
    return "ac-h1"


def cluster_matrix(
    db: Session,
    space_id: int,
    only_kits: list[str] | None = None,
    *,
    linked: bool = True,
    cell_color: str = "cluster",
) -> dict:
    if only_kits is not None:
        kits = list(only_kits)
    else:
        kits = list(
            db.scalars(
                select(GedMatchHit.source_kit)
                .where(GedMatchHit.space_id == space_id)
                .distinct()
                .order_by(GedMatchHit.source_kit)
            )
        )
    names = {row["kit"]: row["name"] for row in correspondence_matrix(db, space_id)["rows"]}
    missing = [kit for kit in kits if not names.get(kit)]
    if missing:
        named = db.scalars(
            select(GedMatchHit)
            .where(
                GedMatchHit.space_id == space_id,
                GedMatchHit.match_kit.in_(missing),
                GedMatchHit.name != "",
            )
            .order_by(GedMatchHit.id)
        )
        for hit in named:
            if not names.get(hit.match_kit):
                names[hit.match_kit] = _kit_display_name(hit.name, "")
    directed: dict[tuple[str, str], float | None] = {}
    generations: dict[tuple[str, str], float | None] = {}
    if kits:
        hits = db.scalars(
            select(GedMatchHit).where(
                GedMatchHit.space_id == space_id,
                GedMatchHit.source_kit.in_(kits),
                GedMatchHit.match_kit.in_(kits),
            )
        )
        for hit in hits:
            directed[(hit.source_kit, hit.match_kit)] = hit.total_cm
            generations[(hit.source_kit, hit.match_kit)] = hit.generations
    clusters = split_loose_clusters(strong_clusters(kits, directed), directed, generations)
    label_of: dict[str, str] = {}
    for index, group in enumerate(clusters, start=1):
        for kit in group:
            label_of[kit] = str(index)
    order = [kit for group in clusters for kit in group]
    rows = []
    previous = None
    for kit in order:
        label = label_of[kit]
        hue = _cluster_hue(label)
        cells = []
        for other in order:
            if kit == other:
                cells.append(
                    {"css": "gedmatch-matrix-self", "href": "", "gen": None, "cm": None, "known": False}
                )
                continue
            if (kit, other) not in directed:
                cells.append({"css": "", "href": "", "gen": None, "cm": None, "known": False})
                continue
            gen = generations.get((kit, other))
            if cell_color == "gen":
                css = gen_tier_class(gen) or ""
            else:
                css = hue if label_of[other] == label else "ac-cross"
            href = ""
            if linked:
                href = (
                    f"/s/{space_id}/gedmatch/clusters/"
                    f"{quote(kit, safe='')}/{quote(other, safe='')}"
                )
            cells.append(
                {
                    "css": css,
                    "href": href,
                    "gen": gen,
                    "cm": directed.get((kit, other)),
                    "known": True,
                }
            )
        rows.append(
            {
                "kit": kit,
                "name": names.get(kit) or "",
                "cluster_label": label,
                "hue": hue,
                "group_start": previous is not None and previous != label,
                "cells": cells,
            }
        )
        previous = label
    return {
        "rows": rows,
        "kits": order,
        "cluster_count": len(clusters),
        "kit_count": len(order),
        "strong_cm": STRONG_CM,
    }


def cluster_match(db: Session, space_id: int, source_kit: str, match_kit: str) -> dict | None:
    source = source_kit.strip().upper()
    match = match_kit.strip().upper()
    hit = db.scalar(
        select(GedMatchHit).where(
            GedMatchHit.space_id == space_id,
            GedMatchHit.source_kit == source,
            GedMatchHit.match_kit == match,
        )
    )
    if hit is None:
        return None
    owner = db.scalar(
        select(GedMatchKit).where(GedMatchKit.space_id == space_id, GedMatchKit.source_kit == source)
    )
    return {
        "source_kit": hit.source_kit,
        "match_kit": hit.match_kit,
        "source_name": _kit_display_name(owner.name if owner else "", ""),
        "name": _kit_display_name(hit.name, ""),
        "total_cm": hit.total_cm,
        "largest_cm": hit.largest_cm,
        "generations": hit.generations,
        "overlap": hit.overlap,
        "date_compared": hit.date_compared,
        "company": hit.company if "@" not in (hit.company or "") else "",
    }


def correspond(db: Session, space_id: int, source_kit: str) -> dict:
    source = source_kit.strip().upper()
    hits = list(
        db.scalars(
            select(GedMatchHit)
            .where(GedMatchHit.space_id == space_id, GedMatchHit.source_kit == source)
            .order_by(GedMatchHit.total_cm.desc(), GedMatchHit.match_kit)
        )
    )
    people = db.execute(
        select(Person, Tree).join(Tree, Tree.id == Person.tree_id).where(Tree.space_id == space_id)
    ).all()
    by_name: dict[str, list[tuple[Person, Tree]]] = defaultdict(list)
    by_id = {}
    for person, tree in people:
        by_id[person.id] = (person, tree)
        if person.name_key:
            by_name[person.name_key].append((person, tree))
    members = db.execute(
        select(ClusterMember, Dataset)
        .join(Dataset, Dataset.id == ClusterMember.dataset_id)
        .where(ClusterMember.space_id == space_id)
    ).all()
    by_cluster: dict[str, list[tuple[ClusterMember, Dataset]]] = defaultdict(list)
    for member, dataset in members:
        key = person_key(member.name)
        if key:
            by_cluster[key].append((member, dataset))

    table = []
    dense: dict[tuple[str, str], list[str]] = defaultdict(list)
    for hit in hits:
        trees = by_name.get(hit.name_key, [])
        clusters = by_cluster.get(person_key(hit.name), [])
        linked = by_id.get(hit.person_id) if hit.person_id else None
        for member, dataset in clusters:
            dense[(dataset.owner_name or "Кластеры", member.cluster_label)].append(hit.name or hit.match_kit)
        table.append(
            {
                "hit": hit,
                "relations": relationship_labels(hit.total_cm),
                "trees": trees[:8],
                "tree_count": len({tree.id for _person, tree in trees}),
                "clusters": clusters[:8],
                "linked": linked,
            }
        )
    groups = [
        {"owner": owner, "label": label, "names": names}
        for (owner, label), names in sorted(dense.items())
        if len(names) >= 2
    ]
    owner_row = db.scalar(
        select(GedMatchKit).where(GedMatchKit.space_id == space_id, GedMatchKit.source_kit == source)
    )
    owner = None
    if owner_row and (owner_row.name or owner_row.email or owner_row.company):
        given, surname = owner_parts(owner_row.name)
        owner = {
            "name": owner_row.name,
            "given": given,
            "surname": surname,
            "email": owner_row.email,
            "company": owner_row.company,
        }
    return {"rows": table, "dense": groups, "total": len(table), "owner": owner}


PARENT_LABEL = "родитель или ребёнок"
SIBLING_LABEL = "брат или сестра"
# One overlapping band. cM does not tell mother's mother from father's mother.
HALF_LABEL = "дед, бабка, внук или внучка, дядя, тётя, племянник или полусиблинг"

# Path code and the older person's role. The younger sex is a separate suffix.
GRAND_PATHS = (
    ("mm", "бабушка (мать матери)"),
    ("mf", "дедушка (отец матери)"),
    ("fm", "бабушка (мать отца)"),
    ("ff", "дедушка (отец отца)"),
)
_GRAND_BY_CODE = {code: elder for code, elder in GRAND_PATHS}
_PARENT_ASSIGNMENTS = (
    ("mo_d", "мать", "дочь"),
    ("mo_s", "мать", "сын"),
    ("fa_d", "отец", "дочь"),
    ("fa_s", "отец", "сын"),
)
_PARENT_BY_CODE = {code: (elder, younger) for code, elder, younger in _PARENT_ASSIGNMENTS}
_GRAND_YOUNGER = {"f": "внучка", "m": "внук", "": "внук или внучка"}
_MUTUAL = (
    ("half", "полусиблинг или та же полоса cM"),
    ("distant", "дальнее родство"),
    ("unrelated", "не родственники"),
)
_MUTUAL_PHRASE = dict(_MUTUAL)
_SIB_WORD = {"br": "брат", "si": "сестра", "m": "брат", "f": "сестра"}

# Generation distance is the gap in birth generations, not the length of the path.
# Parent/child is 1. Full siblings are the same generation, so 0.
# None means the band does not fix a single distance.
_GENERATION_SPAN = {
    PARENT_LABEL: (1, 1),
    SIBLING_LABEL: (0, 0),
    HALF_LABEL: (0, 2),
    "двоюродный, прадед или двоюродный дядя": (0, 3),
    "двоюродный во втором колене": (0, 0),
    "троюродный или дальше": None,
}


def _bounds(label: str) -> tuple[int, int]:
    for name, low, high in RANGES:
        if name == label:
            return low, high
    raise KeyError(label)


def _in_band(cm: float | None, label: str) -> bool:
    if cm is None:
        return False
    low, high = _bounds(label)
    return low <= cm <= high


def _low_or_absent(cm: float | None) -> bool:
    """Missing cM, or below the strong-match floor. Not evidence the two adults share ancestry."""
    return cm is None or cm < STRONG_CM


def _num(value: float | None) -> str:
    if value is None:
        return ""
    text = f"{float(value):.1f}"
    if text.endswith(".0"):
        return text[:-2]
    return text


def measures_from_directed(
    directed_cm: dict[tuple[str, str], float | None],
    directed_gen: dict[tuple[str, str], float | None] | None = None,
) -> dict[tuple[str, str], tuple[float | None, float | None]]:
    """Undirected pair → (stronger cM, Gen from that same direction). A missing side is not invented."""
    gens = directed_gen or {}
    best: dict[tuple[str, str], tuple[float | None, float | None]] = {}
    for (source, match), cm in directed_cm.items():
        if source == match:
            continue
        key = _pair(source, match)
        gen = gens.get((source, match))
        current = best.get(key)
        if current is None or current[0] is None or (cm is not None and cm > current[0]):
            best[key] = (cm, gen)
    return best


def _normalize_measures(
    measures: dict[tuple[str, str], tuple[float | None, float | None]],
) -> dict[tuple[str, str], tuple[float | None, float | None]]:
    best: dict[tuple[str, str], tuple[float | None, float | None]] = {}
    for (left, right), (cm, gen) in measures.items():
        left = left.strip().upper()
        right = right.strip().upper()
        if left == right:
            continue
        key = _pair(left, right)
        current = best.get(key)
        if current is None or current[0] is None or (cm is not None and cm > current[0]):
            best[key] = (cm, gen)
    return best


def parent_groups(
    measures: dict[tuple[str, str], tuple[float | None, float | None]],
) -> list[dict]:
    """Two parent-level matches to one kit, with low or absent cM between those two adults.

    The shared kit is the child. The other two are the parents. No edge is added between
    the parents: kinship is not transitive.
    """
    measures = _normalize_measures(measures)
    partners: dict[str, list[str]] = defaultdict(list)
    for (left, right), (cm, _gen) in measures.items():
        if _in_band(cm, PARENT_LABEL):
            partners[left].append(right)
            partners[right].append(left)
    groups = []
    seen: set[tuple[str, tuple[str, str]]] = set()
    for child, pals in partners.items():
        unique = sorted(set(pals))
        for index, adult in enumerate(unique):
            for other in unique[index + 1 :]:
                pair = _pair(adult, other)
                stored = measures.get(pair)
                cm = stored[0] if stored else None
                if not _low_or_absent(cm):
                    continue
                key = (child, pair)
                if key in seen:
                    continue
                seen.add(key)
                groups.append({"child": child, "parents": pair})
    groups.sort(key=lambda item: (item["child"], item["parents"]))
    return groups


def _generation_by_label(labels: list[str]) -> dict[str, int | tuple[int, int] | None]:
    found: dict[str, int | tuple[int, int] | None] = {}
    for label in labels:
        span = _GENERATION_SPAN.get(label)
        if span is None:
            found[label] = None
        elif span[0] == span[1]:
            found[label] = span[0]
        else:
            found[label] = span
    return found


def _single_distance(labels: list[str]) -> int | None:
    if labels == [PARENT_LABEL]:
        return 1
    if labels == [SIBLING_LABEL]:
        return 0
    return None


# A leading asterisk and an «adm. by …» suffix are display noise, not part of the name.
# Only an obvious given name counts. A surname, a missing name, or an ambiguous token
# stays unknown. Nothing here is written back onto the kit.
_ADM_BY = re.compile(r"\s+adm\.\s*by\b.*$", re.IGNORECASE)
_NAME_EDGE = ".,;:()[]\"'`*"

# Explicit given names seen on these kits, plus the same names in Cyrillic.
# Not a classifier: a token that is not listed stays unknown.
_FEMALE_NAMES = frozenset(
    {
        "nina", "elena", "maria", "mariia", "valentina", "irina", "iryna", "xenia",
        "ksenia", "kseniia", "natalia", "natallia", "yulia", "tatyana", "tatiana",
        "liudmila", "lyudmila", "zinaida", "valiantsina", "luda", "olga", "olha",
        "olena", "volha", "anna", "alena", "alina", "galina", "svetlana",
        "ekaterina", "inna", "daria", "vera", "tamara", "anastasia", "sofia",
        "sophia", "helena", "helen", "mary", "anne", "ann", "alice", "julia",
        "julie", "ruth", "sarah", "linda", "barbara", "elizabeth", "jennifer",
        "susan", "karen", "betty", "sandra", "donna", "carol", "michelle",
        "amanda", "melissa", "deborah", "stephanie", "rebecca", "sharon",
        "kathleen", "katherine", "catherine", "christine", "diana", "diane",
        "angela", "shirley", "virginia", "rachel", "heather", "teresa", "evelyn",
        "gloria", "joan", "marilyn", "natalie", "olivia", "emma", "grace",
        "hannah", "victoria", "kimberly", "jessica", "laura", "amy", "brenda",
        "pamela", "nicole", "nancy", "dorothy", "carolyn", "janet", "andrea",
        "cheryl", "judith", "kathryn",
        "мария", "анна", "ольга", "елена", "нина", "ирина", "наталья", "наталия",
        "татьяна", "юлия", "валентина", "людмила", "галина", "светлана",
        "екатерина", "оксана", "вера", "надежда", "любовь", "дарья", "алена",
        "алина", "зоя", "лариса", "тамара", "ксения", "зинаида",
    }
)
_MALE_NAMES = frozenset(
    {
        "dmitry", "dmitri", "dmitriy", "ivan", "mihail", "mikhail", "viachaslau",
        "yauhen", "jegor", "egor", "aliaksander", "aliaksandr", "alexander",
        "anatoliy", "anatoly", "anatoli", "juri", "yuri", "yury", "stanislav",
        "vladimir", "vlad", "ruslan", "andrey", "andrei", "aleksei", "alexei",
        "alexey", "evgeny", "evgeniy", "egeniy", "oleg", "sergei", "sergey",
        "pavel", "nikolai", "nikolay", "igor", "roman", "maksim", "maxim",
        "kirill", "konstantin", "kostiantyn", "fedor", "fyodor", "grigori",
        "grigory", "petr", "pyotr", "john", "james", "robert", "michael",
        "william", "david", "richard", "joseph", "thomas", "charles",
        "christopher", "daniel", "matthew", "anthony", "donald", "mark", "paul",
        "steven", "andrew", "kenneth", "george", "edward", "brian", "ronald",
        "timothy", "jason", "jeffrey", "ryan", "jacob", "gary", "nicholas",
        "eric", "jonathan", "stephen", "larry", "justin", "scott", "brandon",
        "benjamin", "samuel", "frank", "gregory", "raymond", "patrick", "jack",
        "dennis", "jerry", "tyler", "aaron", "jose", "henry", "adam", "douglas",
        "nathan", "zachary", "peter", "kyle", "walter", "harold", "jeremy",
        "ethan", "carl", "keith", "roger", "gerald", "lawrence", "sean",
        "christian", "austin", "joe", "juan", "albert", "wayne", "ralph", "roy",
        "eugene", "louis", "philip", "bobby", "johnny", "bradley", "vincent",
        "иван", "дмитрий", "михаил", "андрей", "александр", "сергей", "олег",
        "павел", "николай", "владимир", "руслан", "анатолий", "станислав",
        "егор", "юрий", "петр", "федор", "игорь", "роман", "максим", "кирилл",
        "константин", "алексей", "евгений", "виктор", "василий", "григорий",
        "борис", "леонид", "геннадий", "вячеслав",
    }
)
# Same spelling can be either sex, so the token never assigns one.
_AMBIGUOUS_NAMES = frozenset(
    {
        "alex", "sasha", "sascha", "valery", "valerii", "valeriy", "zhenya",
        "zhenia", "kim", "robin", "lee", "pat", "sam", "casey", "jordan",
        "taylor", "nika", "nat", "jean", "francis", "leslie", "shannon",
        "алекс", "саша", "женя", "валерий",
    }
)

_PARENT_WORD = {"f": "мать", "m": "отец", "": "родитель"}
_CHILD_WORD = {"f": "дочь", "m": "сын", "": "ребёнок"}
_GRAND_WORD = {"f": "бабушка", "m": "дедушка", "": "бабушка или дедушка"}
_GRANDCHILD_WORD = {"f": "внучка", "m": "внук", "": "внук или внучка"}


def _norm_sex(value: str) -> str:
    token = (value or "").strip().lower()
    if token in {"f", "female", "ж"}:
        return "f"
    if token in {"m", "male", "м"}:
        return "m"
    upper = (value or "").strip().upper()
    if upper == "F":
        return "f"
    if upper == "M":
        return "m"
    return ""


def sex_from_name(name: str) -> str:
    """Return 'f', 'm', or ''.

    Strip a leading asterisk and an «adm. by …» suffix, then look only at
    obvious Russian, Belarusian, and English given names. A surname alone,
    a missing name, or an ambiguous given name stays unknown. Do not guess
    from the surname. The caller must not store the result.
    """
    text = (name or "").strip().lstrip("*").strip()
    text = _ADM_BY.sub("", text)
    found: list[str] = []
    for raw in text.split():
        token = raw.strip(_NAME_EDGE)
        if len(token) < 3 or any(ch.isdigit() for ch in token):
            continue
        key = token.casefold().replace("ё", "е")
        if key in _AMBIGUOUS_NAMES:
            continue
        sex = "f" if key in _FEMALE_NAMES else "m" if key in _MALE_NAMES else ""
        if sex and sex not in found:
            found.append(sex)
    if len(found) == 1:
        return found[0]
    return ""


def _sex_of(sexes: dict[str, str] | None, kit: str) -> str:
    return _norm_sex((sexes or {}).get(kit, ""))


def _sibling_word(left: str, right: str) -> str:
    if left == "f" and right == "f":
        return "сёстры"
    if left == "m" and right == "m":
        return "братья"
    if left and right:
        return "брат и сестра"
    return ""


def _parent_lines(
    source: str, match: str, sexes: dict[str, str] | None, names: dict[str, str] | None
) -> list[str]:
    if not source or not match:
        return []
    left = _sex_of(sexes, source)
    right = _sex_of(sexes, match)
    if not left and not right:
        return []
    return [
        _assignment(source, _PARENT_WORD[left], match, _CHILD_WORD[right], names),
        _assignment(match, _PARENT_WORD[right], source, _CHILD_WORD[left], names),
    ]


# Hits store total cM, largest segment, and Gen. There is no X-DNA column,
# so mother's mother is not preferred over father's mother.
def _grand_lines(
    source: str, match: str, sexes: dict[str, str] | None, names: dict[str, str] | None
) -> list[str]:
    if not source or not match:
        return []
    left = _sex_of(sexes, source)
    right = _sex_of(sexes, match)
    if not left and not right:
        return []
    lines = []
    for elder, elder_sex, younger, younger_sex in (
        (source, left, match, right),
        (match, right, source, left),
    ):
        lines.append(
            _assignment(
                elder,
                _GRAND_WORD[elder_sex],
                younger,
                _GRANDCHILD_WORD[younger_sex],
                names,
            )
        )
    return lines


def _directed_parent_sentence(
    parent: str, child: str, sexes: dict[str, str] | None, names: dict[str, str] | None
) -> str:
    parent_sex = _sex_of(sexes, parent)
    child_sex = _sex_of(sexes, child)
    if not parent_sex and not child_sex:
        return ""
    return _assignment(parent, _PARENT_WORD[parent_sex], child, _CHILD_WORD[child_sex], names) + "."


def _two_parents_sex_line(
    parents: tuple[str, str],
    child: str,
    sexes: dict[str, str] | None,
    names: dict[str, str] | None,
) -> str:
    first, second = parents
    if not any(_sex_of(sexes, kit) for kit in (first, second, child)):
        return ""
    left = f"{_kit_with_name(first, names)} — {_PARENT_WORD[_sex_of(sexes, first)]}"
    right = f"{_kit_with_name(second, names)} — {_PARENT_WORD[_sex_of(sexes, second)]}"
    kid = f"{_kit_with_name(child, names)} — {_CHILD_WORD[_sex_of(sexes, child)]}"
    return f"{left}, {right}. {kid}."


def _generation_phrase(label: str) -> str:
    span = _GENERATION_SPAN.get(label)
    if span is None:
        return "число поколений не фиксируется"
    low, high = span
    if low == high:
        word = "поколение" if low == 1 else "поколений"
        return f"{low} {word}"
    return f"от {low} до {high} поколений"


def _hypothesis_summary(
    labels: list[str],
    source: str = "",
    match: str = "",
    sexes: dict[str, str] | None = None,
    names: dict[str, str] | None = None,
) -> str:
    sibling = _sibling_word(_sex_of(sexes, source), _sex_of(sexes, match))

    def shown(label: str) -> str:
        if label == SIBLING_LABEL and sibling:
            return sibling
        return label

    if not labels:
        text = "По общему cM близкое родство не выводится."
    elif len(labels) == 1:
        label = labels[0]
        span = _GENERATION_SPAN.get(label)
        if label == PARENT_LABEL:
            text = (
                "Общий cM попадает в полосу «родитель или ребёнок». "
                "Расстояние в поколениях: 1. "
                "Кто родитель, а кто ребёнок, по одной паре не видно."
            )
        elif span is None:
            text = (
                f"Общий cM попадает в полосу «{shown(label)}». "
                "Число поколений по этой полосе не фиксируется."
            )
        elif span[0] == span[1]:
            text = f"Общий cM попадает в полосу «{shown(label)}». Расстояние в поколениях: {span[0]}."
        else:
            text = (
                f"Общий cM попадает в полосу «{shown(label)}». "
                f"Расстояние в поколениях: от {span[0]} до {span[1]}."
            )
    else:
        parts = [f"{shown(label)} ({_generation_phrase(label)})" for label in labels]
        text = "Общий cM подходит нескольким гипотезам: " + "; ".join(parts) + "."
    if PARENT_LABEL in labels:
        lines = _parent_lines(source, match, sexes, names)
        if lines:
            sentence = "Открытые гипотезы: " + "; ".join(lines) + "."
            old = "Кто родитель, а кто ребёнок, по одной паре не видно."
            if old in text:
                text = text.replace(old, sentence)
            else:
                text += " " + sentence
    if HALF_LABEL in labels:
        lines = _grand_lines(source, match, sexes, names)
        if lines:
            text += " Если это дед или бабка, линия по cM не выбирается: " + "; ".join(lines) + "."
    return text


def _gen_note(gen: float | None) -> str:
    if gen is None:
        return ""
    return f" Gen по GedMatch: {_num(gen)}."


def parent_hint_lines(measures: dict[tuple[str, str], tuple[float | None, float | None]]) -> list[str]:
    lines = []
    for group in parent_groups(measures):
        child = group["child"]
        first, second = group["parents"]
        lines.append(
            f"Набор {child} может быть ребёнком наборов {first} и {second}. "
            f"Расстояние до каждого родителя — 1 поколение. "
            f"Между {first} и {second} родственная связь не выводится."
        )
    return lines


def forecast_pair(
    measures: dict[tuple[str, str], tuple[float | None, float | None]],
    source: str,
    match: str,
    sexes: dict[str, str] | None = None,
    names: dict[str, str] | None = None,
) -> dict:
    """Relationship hypotheses for one pair. Does not add an edge between two parents of the same child.

    sexes maps a kit code to 'f', 'm', or ''. A missing sex stays unsexed.
    The map is computed by the caller and is not stored.
    """
    source = source.strip().upper()
    match = match.strip().upper()
    measures = _normalize_measures(measures)
    pair = _pair(source, match)
    stored = measures.get(pair)
    cm, gen = stored if stored else (None, None)
    groups = parent_groups(measures)
    as_parents = next((group for group in groups if group["parents"] == pair), None)
    if as_parents is not None:
        child = as_parents["child"]
        if cm is None:
            amount = "Общего cM между ними нет."
        else:
            amount = f"Общий cM {_num(cm)} для родства между ними мал."
        summary = (
            f"Между этими наборами родственная связь не выводится: "
            f"это двое родителей набора {child}. {amount} "
            f"Расстояние в поколениях до ребёнка: 1."
            f"{_gen_note(gen)}"
        )
        sex_line = _two_parents_sex_line(as_parents["parents"], child, sexes, names)
        if sex_line:
            summary += " " + sex_line
        return {
            "labels": [],
            "generation_by_label": {},
            "generation_distance": None,
            "kinship_edge": False,
            "role": "two_parents",
            "child": child,
            "parents": as_parents["parents"],
            "summary": summary,
        }

    other_parents: list[str] = []
    role = ""
    for group in groups:
        parents = group["parents"]
        child = group["child"]
        if source == child and match in parents:
            role = "child"
            other_parents.append(parents[0] if parents[1] == match else parents[1])
        elif match == child and source in parents:
            role = "parent"
            other_parents.append(parents[0] if parents[1] == source else parents[1])
    if role:
        labels = [PARENT_LABEL]
        unique_others = list(dict.fromkeys(other_parents))
        child_kit = match if role == "parent" else source
        parent_kit = source if role == "parent" else match
        directed = _directed_parent_sentence(parent_kit, child_kit, sexes, names)
        if directed:
            who = directed
        elif role == "parent":
            who = "Этот набор — родитель, совпадение — ребёнок."
        else:
            who = "Этот набор — ребёнок, совпадение — один из родителей."
        extra = ""
        if unique_others:
            shown = " и ".join(unique_others)
            if role == "parent":
                pair_note = f"Этот набор и {shown} — двое родителей совпадения."
            else:
                pair_note = f"Совпадение и {shown} — двое родителей этого набора."
            extra = (
                f" Второй родитель — {shown}. {pair_note} "
                "Между родителями родственная связь не выводится."
            )
        summary = f"{who} Расстояние в поколениях: 1.{extra}{_gen_note(gen)}"
        other_words = []
        for other in unique_others:
            other_sex = _sex_of(sexes, other)
            if other_sex:
                other_words.append(f"{_kit_with_name(other, names)} — {_PARENT_WORD[other_sex]}.")
        if other_words:
            summary += " " + " ".join(other_words)
        return {
            "labels": labels,
            "generation_by_label": {PARENT_LABEL: 1},
            "generation_distance": 1,
            "kinship_edge": True,
            "role": role,
            "child": child_kit,
            "parents": tuple(sorted({parent_kit, *unique_others})),
            "summary": summary,
        }

    labels = relationship_labels(cm)
    by_label = _generation_by_label(labels)
    distance = _single_distance(labels)
    if not labels and cm is None:
        summary = "По этим наборам нет общего cM, прогноз родства не строится."
    else:
        summary = _hypothesis_summary(labels, source, match, sexes, names) + _gen_note(gen)
    return {
        "labels": labels,
        "generation_by_label": by_label,
        "generation_distance": distance,
        "kinship_edge": bool(labels),
        "role": "",
        "child": "",
        "parents": (),
        "summary": summary,
    }


def _kits_for_forecast(
    measures: dict[tuple[str, str], tuple[float | None, float | None]], source: str, match: str
) -> list[str]:
    kits = {source, match}
    for group in parent_groups(measures):
        involved = {group["child"], *group["parents"]}
        if source in involved or match in involved:
            kits |= involved
    return sorted(kits)


def _stored_kit_sexes(db: Session, space_id: int, kits: list[str]) -> dict[str, str]:
    """Sex already stored on the kit row. A blank cell is not a guess."""
    if not kits:
        return {}
    rows = db.execute(
        select(GedMatchKit.source_kit, GedMatchKit.sex).where(
            GedMatchKit.space_id == space_id,
            GedMatchKit.source_kit.in_(kits),
        )
    ).all()
    found: dict[str, str] = {}
    for kit, sex in rows:
        code = _norm_sex(sex or "")
        if code:
            found[kit] = code
    return found


def _linked_person_sexes(db: Session, space_id: int, kits: list[str]) -> tuple[dict[str, str], set[str]]:
    """Sex stored on a linked tree person. Do not copy it onto the kit."""
    if not kits:
        return {}, set()
    rows = db.execute(
        select(GedMatchHit.match_kit, Person.sex)
        .join(Person, Person.id == GedMatchHit.person_id)
        .where(
            GedMatchHit.space_id == space_id,
            GedMatchHit.match_kit.in_(kits),
            GedMatchHit.person_id.is_not(None),
        )
    ).all()
    found: dict[str, str] = {}
    conflict: set[str] = set()
    for kit, sex in rows:
        code = _norm_sex(sex or "")
        if not code:
            continue
        previous = found.get(kit)
        if previous and previous != code:
            conflict.add(kit)
        else:
            found[kit] = code
    for kit in conflict:
        found.pop(kit, None)
    return found, conflict


def resolve_kit_sexes(
    db: Session, space_id: int, kits: list[str], names: dict[str, str]
) -> dict[str, str]:
    """Kit sex, then linked person sex, then an obvious given name.

    The name guess is not written back. A blank kit sex stays blank.
    """
    kit_sex = _stored_kit_sexes(db, space_id, kits)
    person_sex, blocked = _linked_person_sexes(db, space_id, kits)
    sexes: dict[str, str] = {}
    for kit in kits:
        if kit in kit_sex:
            sexes[kit] = kit_sex[kit]
        elif kit in person_sex:
            sexes[kit] = person_sex[kit]
        elif kit in blocked:
            sexes[kit] = ""
        else:
            sexes[kit] = sex_from_name(names.get(kit, ""))
    return sexes


def forecast_for_pair(
    db: Session,
    space_id: int,
    measures: dict[tuple[str, str], tuple[float | None, float | None]],
    source: str,
    match: str,
) -> tuple[dict, dict[str, str]]:
    source = source.strip().upper()
    match = match.strip().upper()
    kits = _kits_for_forecast(measures, source, match)
    names = header_names_for(db, space_id, kits)
    sexes = resolve_kit_sexes(db, space_id, kits, names)
    return forecast_pair(measures, source, match, sexes, names), names


def _kit_with_name(code: str, names: dict[str, str] | None) -> str:
    name = ((names or {}).get(code) or "").strip()
    if not name:
        return code
    return f"{code} ({name})"


def _assignment(
    left_kit: str,
    left_role: str,
    right_kit: str,
    right_role: str,
    names: dict[str, str] | None = None,
) -> str:
    left = _kit_with_name(left_kit, names)
    right = _kit_with_name(right_kit, names)
    return f"{left} — {left_role}, {right} — {right_role}"


def _is_sib_code(relation: str) -> bool:
    return (
        len(relation) == 6
        and relation.startswith("sib_")
        and relation[4] in "mf"
        and relation[5] in "mf"
    )


def _parse_stored_grand(relation: str) -> tuple[str, str] | None:
    """Path code and younger sex. Empty sex is a row saved before sexes were stored."""
    if not relation.startswith("gp_"):
        return None
    body = relation[3:]
    if body in _GRAND_BY_CODE:
        return body, ""
    if len(body) == 4 and body[2] == "_" and body[:2] in _GRAND_BY_CODE and body[3] in "fm":
        return body[:2], body[3]
    return None


def _known_kinship(relation: str) -> bool:
    if relation in _PARENT_BY_CODE or relation in ("parent", "sibling", "half", "distant", "unrelated"):
        return True
    if _is_sib_code(relation):
        return True
    return _parse_stored_grand(relation) is not None


def _assignment_fits(relation: str, parent_kit: str, source: str, match: str) -> bool:
    if not source or not match or source == match:
        return False
    if relation in ("half", "distant", "unrelated", "sibling") or _is_sib_code(relation):
        return True
    if relation in _PARENT_BY_CODE or relation == "parent" or _parse_stored_grand(relation) is not None:
        return parent_kit in (source, match)
    return False


def _form_grand_code(code: str) -> tuple[str, str] | None:
    if len(code) != 4 or code[2] != "_":
        return None
    path, sex = code[:2], code[3]
    if path in _GRAND_BY_CODE and sex in "fm":
        return path, sex
    return None


def _sibling_relation(left_word: str, right_word: str, source: str, match: str) -> str:
    sex = {"br": "m", "si": "f"}
    low, high = _pair(source, match)
    by_kit = {source: sex[left_word], match: sex[right_word]}
    return f"sib_{by_kit[low]}{by_kit[high]}"


def _side_choice(code: str, elder: str, source: str, match: str) -> str:
    if elder == source:
        return f"{code}:L"
    if elder == match:
        return f"{code}:R"
    return ""


def _sibling_choice(relation: str, source: str, match: str) -> str:
    low, high = _pair(source, match)
    by_kit = {low: relation[4], high: relation[5]}
    word = {"m": "br", "f": "si"}
    return f"{word[by_kit[source]]}_{word[by_kit[match]]}"


def _manual_distance(relation: str) -> int | None:
    if relation in _PARENT_BY_CODE or relation == "parent":
        return 1
    if _parse_stored_grand(relation) is not None:
        return 2
    if relation == "sibling" or _is_sib_code(relation):
        return 0
    return None


def kinship_form_options(
    left: str, right: str, names: dict[str, str] | None = None
) -> list[dict[str, str]]:
    """One option per complete assignment. Each label names both kits."""
    left = left.strip().upper()
    right = right.strip().upper()
    options: list[dict[str, str]] = []
    for code, elder_role, younger_role in _PARENT_ASSIGNMENTS:
        options.append(
            {"value": f"{code}:L", "label": _assignment(left, elder_role, right, younger_role, names)}
        )
        options.append(
            {"value": f"{code}:R", "label": _assignment(right, elder_role, left, younger_role, names)}
        )
    for path, elder_role in GRAND_PATHS:
        for sex, younger_role in (("f", "внучка"), ("m", "внук")):
            code = f"{path}_{sex}"
            options.append(
                {"value": f"{code}:L", "label": _assignment(left, elder_role, right, younger_role, names)}
            )
            options.append(
                {"value": f"{code}:R", "label": _assignment(right, elder_role, left, younger_role, names)}
            )
    for left_sex, right_sex in (("br", "si"), ("si", "br"), ("br", "br"), ("si", "si")):
        options.append(
            {
                "value": f"{left_sex}_{right_sex}",
                "label": _assignment(left, _SIB_WORD[left_sex], right, _SIB_WORD[right_sex], names),
            }
        )
    for code, phrase in _MUTUAL:
        pair = f"{_kit_with_name(left, names)} и {_kit_with_name(right, names)}"
        options.append({"value": code, "label": f"{pair} — {phrase}"})
    return options


def store_kinship_choice(choice: str, source: str, match: str) -> tuple[str, str] | None:
    """One canonical row. Swapping who is older updates parent_kit, it does not add a row."""
    choice = choice.strip()
    source = source.strip().upper()
    match = match.strip().upper()
    if not source or not match or source == match:
        return None
    if choice in _MUTUAL_PHRASE:
        return choice, ""
    parts = choice.split("_")
    if len(parts) == 2 and parts[0] in ("br", "si") and parts[1] in ("br", "si"):
        return _sibling_relation(parts[0], parts[1], source, match), ""
    if choice.count(":") != 1:
        return None
    code, side = choice.split(":")
    if side not in ("L", "R"):
        return None
    elder = source if side == "L" else match
    if code in _PARENT_BY_CODE:
        return code, elder
    grand = _form_grand_code(code)
    if grand is None:
        return None
    path, sex = grand
    return f"gp_{path}_{sex}", elder


def _saved_choice(relation: str, parent_kit: str, source: str, match: str) -> str:
    if relation in _MUTUAL_PHRASE:
        return relation
    if relation in _PARENT_BY_CODE:
        return _side_choice(relation, parent_kit, source, match)
    grand = _parse_stored_grand(relation)
    if grand is not None:
        path, sex = grand
        if not sex:
            return ""
        return _side_choice(f"{path}_{sex}", parent_kit, source, match)
    if _is_sib_code(relation):
        return _sibling_choice(relation, source, match)
    return ""


def _saved_sentence(relation: str, phrase: str, distance: int | None) -> str:
    if relation == "half":
        return f"Сохранённая связь: {phrase}. Расстояние в поколениях: от 0 до 2."
    if distance is None:
        return f"Сохранённая связь: {phrase}."
    return f"Сохранённая связь: {phrase}. Расстояние в поколениях: {distance}."


def apply_manual(
    forecast: dict,
    relation: str,
    parent_kit: str,
    source: str,
    match: str,
    names: dict[str, str] | None = None,
) -> dict:
    """Saved link is the conclusion. The cM forecast stays on hypothesis."""
    source = source.strip().upper()
    match = match.strip().upper()
    parent_kit = parent_kit.strip().upper()
    relation = relation.strip()
    hypothesis = {
        "labels": list(forecast["labels"]),
        "generation_by_label": dict(forecast["generation_by_label"]),
        "generation_distance": forecast["generation_distance"],
        "kinship_edge": forecast["kinship_edge"],
        "role": forecast["role"],
        "summary": forecast["summary"],
    }
    if not _known_kinship(relation) or not _assignment_fits(relation, parent_kit, source, match):
        relation = "unrelated"
        parent_kit = ""
    low, high = _pair(source, match)
    phrase = stored_kinship_label(relation, low, high, parent_kit, names)
    distance = _manual_distance(relation)
    merged = dict(forecast)
    merged["hypothesis"] = hypothesis
    merged["conclusion_relation"] = relation
    merged["generation_distance"] = distance
    merged["saved_sentence"] = _saved_sentence(relation, phrase, distance)
    merged["saved_choice"] = _saved_choice(relation, parent_kit, source, match)
    return merged


def stored_kinship_label(
    relation: str,
    kit_low: str,
    kit_high: str,
    parent_kit: str,
    names: dict[str, str] | None = None,
) -> str:
    """Who is who. For sib_* the kit arguments are kit_low, then kit_high."""
    kit_low = kit_low.strip().upper()
    kit_high = kit_high.strip().upper()
    parent_kit = parent_kit.strip().upper()
    pair = f"{_kit_with_name(kit_low, names)} и {_kit_with_name(kit_high, names)}"
    if relation in _PARENT_BY_CODE and parent_kit in (kit_low, kit_high):
        elder_role, younger_role = _PARENT_BY_CODE[relation]
        younger = kit_high if parent_kit == kit_low else kit_low
        return _assignment(parent_kit, elder_role, younger, younger_role, names)
    if relation == "parent" and parent_kit in (kit_low, kit_high):
        younger = kit_high if parent_kit == kit_low else kit_low
        return _assignment(parent_kit, "родитель", younger, "ребёнок", names)
    grand = _parse_stored_grand(relation)
    if grand is not None and parent_kit in (kit_low, kit_high):
        path, sex = grand
        younger = kit_high if parent_kit == kit_low else kit_low
        return _assignment(parent_kit, _GRAND_BY_CODE[path], younger, _GRAND_YOUNGER[sex], names)
    if _is_sib_code(relation):
        return _assignment(kit_low, _SIB_WORD[relation[4]], kit_high, _SIB_WORD[relation[5]], names)
    if relation == "sibling":
        return f"{pair} — брат или сестра"
    if relation in _MUTUAL_PHRASE:
        return f"{pair} — {_MUTUAL_PHRASE[relation]}"
    return f"{pair} — не родственники"
