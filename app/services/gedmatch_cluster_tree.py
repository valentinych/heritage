from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Family, GedMatchHit, Person, Tree, family_child
from app.services.cluster_self import person_key
from app.services.cluster_tree import BUS_DROP, BUS_GAP, GAP, PAD, ROOT_W, _clip, _node

COL_W = 128
ROW_GAP = 28
SECTION_GAP = 48


def agreed_links(pairs: list[tuple[str, int]]) -> dict[str, int]:
    """One person per kit. Disagreeing links are dropped instead of guessed."""
    found: dict[str, set[int]] = {}
    for kit, person_id in pairs:
        found.setdefault(kit, set()).add(person_id)
    return {kit: next(iter(ids)) for kit, ids in found.items() if len(ids) == 1}


def clusters_from_rows(rows: list[dict]) -> list[dict]:
    grouped: list[dict] = []
    current: dict | None = None
    for row in rows:
        label = str(row["cluster_label"])
        if current is None or current["label"] != label:
            current = {"label": label, "kits": []}
            grouped.append(current)
        current["kits"].append(row["kit"])
    return grouped


def count_linked_clusters(db: Session, space_id: int, rows: list[dict]) -> int:
    grouped = clusters_from_rows(rows)
    if not grouped:
        return 0
    links = _load_links(db, space_id, [kit for group in grouped for kit in group["kits"]])
    valid = {person["id"] for person in _load_tree(db, space_id, link_people=False)[0]}
    return sum(1 for group in grouped if any(links.get(kit) in valid for kit in group["kits"]))


def scheme_for_rows(
    db: Session,
    space_id: int,
    rows: list[dict],
    focus_name: str = "",
    *,
    link_people: bool = True,
) -> dict | None:
    grouped = clusters_from_rows(rows)
    if not grouped:
        return None
    links = _load_links(db, space_id, [kit for group in grouped for kit in group["kits"]])
    people, families = _load_tree(db, space_id, link_people=link_people)
    valid = {person["id"] for person in people}
    prepared = []
    for group in grouped:
        person_ids = []
        for kit in group["kits"]:
            person_id = links.get(kit)
            if person_id in valid and person_id not in person_ids:
                person_ids.append(person_id)
        prepared.append({"label": group["label"], "size": len(group["kits"]), "people": person_ids})
    focus = _focus_id(people, families, focus_name)
    root_name = next((person["name"] for person in people if person["id"] == focus), "")
    return layout_gedmatch_clusters(prepared, people, families, focus, root_name)


def layout_gedmatch_clusters(
    clusters: list[dict],
    people: list[dict],
    families: list[dict],
    focus_id: int | None = None,
    root_name: str = "",
) -> dict | None:
    """Tester on top, then real tree links, then clusters placed along those branches."""
    if not clusters:
        return None
    by_id, parents, spouses = _graph(people, families)
    if focus_id not in by_id:
        focus_id = None
    ancestors, depths = _ancestry(parents)
    father, mother, paternal, maternal = _sides(focus_id, by_id, parents, spouses, ancestors)
    prepared = []
    for cluster in clusters:
        person_ids = [person_id for person_id in cluster.get("people") or [] if person_id in by_id]
        prepared.append(
            {
                "label": str(cluster["label"]),
                "size": int(cluster.get("size", len(person_ids))),
                "people": person_ids,
            }
        )
    buckets = {"pat": [], "bridge": [], "mat": [], "kin": [], "other": [], "unlinked": []}
    for cluster in prepared:
        buckets[_bucket(cluster, focus_id, paternal, maternal, ancestors)].append(cluster)

    def meeting(person: int, other: int) -> int | None:
        common = ancestors(person) & ancestors(other)
        if not common:
            return None
        return max(common, key=lambda person_id: (depths[person_id], -person_id))

    def lca(person_ids: list[int]) -> int | None:
        if not person_ids:
            return None
        common: set[int] | None = None
        for person_id in person_ids:
            found = set(ancestors(person_id))
            common = found if common is None else common & found
        if not common:
            return None
        return max(common, key=lambda person_id: (depths[person_id], -person_id))

    def side_anchor(cluster: dict, side_ids: set[int], side_parent: int | None) -> int | None:
        chosen = [person_id for person_id in cluster["people"] if person_id in side_ids]
        shared = lca(chosen)
        if shared is None or side_parent is None:
            return None
        return meeting(shared, side_parent)

    sections = []
    if buckets["pat"] and father is not None:
        sections.append(
            _headed(
                "По отцу" if by_id[father].get("sex") == "M" else "По родителю",
                father,
                _pack(buckets["pat"], lambda cluster: side_anchor(cluster, paternal, father), father, depths),
                connect=True,
            )
        )
    if buckets["bridge"]:
        sections.append(_floating(buckets["bridge"]))
    if buckets["mat"] and mother is not None:
        sections.append(
            _headed(
                "По матери" if by_id[mother].get("sex") == "F" else "По родителю",
                mother,
                _pack(buckets["mat"], lambda cluster: side_anchor(cluster, maternal, mother), mother, depths),
                connect=True,
            )
        )
    kin_groups = _kin_groups(buckets["kin"], focus_id, parents, ancestors, depths, lca, meeting)
    if kin_groups:
        sections.append({"header": None, "connect": False, "groups": kin_groups})
    if buckets["other"]:
        sections.append({"header": None, "connect": False, "groups": _shared_groups(buckets["other"], ancestors, depths)})
    if buckets["unlinked"]:
        sections.append(
            {
                "header": {"title": "Без дерева", "person_id": None, "kind": "twig"},
                "connect": False,
                "groups": [_group(None, buckets["unlinked"], edge=False, card=False)],
            }
        )
    if not sections:
        return None
    return _draw(sections, by_id, root_name, focus_id)


def _bucket(cluster: dict, focus: int | None, paternal: set[int], maternal: set[int], ancestors) -> str:
    people = cluster["people"]
    if not people:
        return "unlinked"
    kinds = set()
    for person_id in people:
        if person_id in paternal:
            kinds.add("pat")
        elif person_id in maternal:
            kinds.add("mat")
        elif focus is not None and (focus in ancestors(person_id) or person_id in ancestors(focus)):
            kinds.add("kin")
        else:
            kinds.add("other")
    if "pat" in kinds and "mat" in kinds:
        return "bridge"
    for name in ("pat", "mat", "kin", "other"):
        if name in kinds:
            return name
    return "unlinked"


def _headed(title: str, person_id: int, groups: list[dict], *, connect: bool) -> dict:
    return {
        "header": {"title": title, "person_id": person_id, "kind": "branch"},
        "connect": connect,
        "groups": groups,
    }


def _floating(clusters: list[dict]) -> dict:
    return {"header": None, "connect": False, "groups": [_group(None, clusters, edge=False, card=False)]}


def _group(anchor: int | None, clusters: list[dict], *, edge: bool, card: bool, from_root: bool = False) -> dict:
    ordered = sorted(clusters, key=lambda cluster: _label_key(cluster["label"]))
    return {"anchor": anchor, "edge": edge, "card": card, "from_root": from_root, "clusters": ordered}


def _pack(clusters: list[dict], anchor_of, side_parent: int, depths: dict[int, int]) -> list[dict]:
    buckets: dict[int | None, list[dict]] = {}
    for cluster in clusters:
        anchor = anchor_of(cluster)
        buckets.setdefault(anchor, []).append(cluster)

    def sort_key(anchor: int | None) -> tuple:
        if anchor is None:
            return (1, 0, 0)
        return (0, depths.get(anchor, 0), anchor)

    groups = []
    for anchor in sorted(buckets, key=sort_key):
        groups.append(_group(anchor, buckets[anchor], edge=anchor is not None, card=anchor not in (None, side_parent)))
    return groups


def _kin_groups(clusters, focus, parents, ancestors, depths, lca, meeting) -> list[dict]:
    if not clusters or focus is None:
        return _shared_groups(clusters, ancestors, depths) if clusters else []
    direct: list[dict] = []
    cards: dict[int, list[dict]] = {}
    floating: list[dict] = []
    for cluster in clusters:
        mode, anchor = _kin_anchor(cluster, focus, parents, ancestors, lca, meeting)
        if mode == "direct":
            direct.append(cluster)
        elif mode == "card" and anchor is not None:
            cards.setdefault(anchor, []).append(cluster)
        else:
            floating.append(cluster)
    groups = []
    if direct:
        groups.append(_group(focus, direct, edge=True, card=False, from_root=True))
    for anchor, items in sorted(cards.items(), key=lambda item: (depths.get(item[0], 0), item[0])):
        groups.append(_group(anchor, items, edge=True, card=True, from_root=True))
    if floating:
        groups.append(_group(None, floating, edge=False, card=False))
    return groups


def _kin_anchor(cluster, focus, parents, ancestors, lca, meeting) -> tuple[str, int | None]:
    people = cluster["people"]
    if not people or all(person_id == focus for person_id in people):
        return "none", None
    if all(focus in parents.get(person_id, []) or person_id in parents.get(focus, []) for person_id in people):
        return "direct", None
    branches = [_child_of_focus(person_id, focus, parents, ancestors) for person_id in people]
    if branches and all(branch is not None and branch == branches[0] for branch in branches):
        child = branches[0]
        if all(person_id == child for person_id in people):
            return "direct", None
        return "card", child
    shared = lca(people)
    if shared is None:
        return "none", None
    found = meeting(shared, focus)
    if found is None or found == focus:
        return "none", None
    return "card", found


def _child_of_focus(person: int, focus: int, parents, ancestors) -> int | None:
    if person == focus or focus not in ancestors(person):
        return None
    current = person
    seen: set[int] = set()
    while current not in seen:
        seen.add(current)
        folks = parents.get(current) or []
        if focus in folks:
            return current
        nxt = next((parent for parent in folks if parent != focus and focus in ancestors(parent)), None)
        if nxt is None:
            return None
        current = nxt
    return None


def _shared_groups(clusters: list[dict], ancestors, depths: dict[int, int]) -> list[dict]:
    if not clusters:
        return []

    def anchor_of(cluster: dict) -> int | None:
        common: set[int] | None = None
        for person_id in cluster["people"]:
            found = set(ancestors(person_id))
            common = found if common is None else common & found
        if not common:
            return None
        best = None
        for ancestor in common:
            shared = any(
                ancestor in ancestors(person_id)
                for other in clusters
                if other is not cluster
                for person_id in other["people"]
            )
            if not shared:
                continue
            rank = (depths[ancestor], -ancestor)
            if best is None or rank > best[0]:
                best = (rank, ancestor)
        return None if best is None else best[1]

    buckets: dict[int | None, list[dict]] = {}
    for cluster in clusters:
        buckets.setdefault(anchor_of(cluster), []).append(cluster)

    def sort_key(anchor: int | None) -> tuple:
        if anchor is None:
            return (1, 0, 0)
        return (0, depths.get(anchor, 0), anchor)

    return [
        _group(anchor, buckets[anchor], edge=anchor is not None, card=anchor is not None)
        for anchor in sorted(buckets, key=sort_key)
    ]


def _draw(sections: list[dict], by_id: dict[int, dict], root_name: str, focus_id: int | None) -> dict:
    has_card = any(group["card"] for section in sections for group in section["groups"])
    header_h = 0
    card_h = 0
    for section in sections:
        header = section["header"]
        if header is not None:
            header_h = max(header_h, _block_height(_header_lines(header, by_id, COL_W)))
        for group in section["groups"]:
            if group["card"] and group["anchor"] in by_id:
                card_h = max(card_h, _block_height(_ancestor_lines(by_id[group["anchor"]]["name"], COL_W)))
    root_lines = _root_lines(root_name)
    root = _box("root", PAD, PAD, ROOT_W, root_lines, _label(root_lines), href=_href(by_id, focus_id))
    header_y = root["y"] + root["h"] + BUS_DROP + BUS_GAP
    if header_h and has_card:
        card_y = header_y + header_h + ROW_GAP
        cluster_y = card_y + card_h + ROW_GAP
    elif header_h:
        card_y = header_y
        cluster_y = header_y + header_h + ROW_GAP
    elif has_card:
        card_y = header_y
        cluster_y = card_y + card_h + ROW_GAP
    else:
        card_y = header_y
        cluster_y = header_y

    cursor = PAD
    for index, section in enumerate(sections):
        if index:
            cursor += SECTION_GAP
        section["x"] = cursor
        for group_index, group in enumerate(section["groups"]):
            if group_index:
                cursor += GAP
            group["x"] = cursor
            for cluster_index, cluster in enumerate(group["clusters"]):
                if cluster_index:
                    cursor += GAP
                cluster["x"] = cursor
                cursor += COL_W
            group["right"] = cursor
        section["right"] = cursor

    nodes = [root]
    paths: list[dict] = []
    root_targets: list[tuple[float, float]] = []
    for section in sections:
        header = section["header"]
        header_node = None
        if header is not None and section["right"] > section["x"]:
            width = section["right"] - section["x"]
            lines = _header_lines(header, by_id, width)
            person = by_id.get(header["person_id"]) if header["person_id"] else None
            header_node = _box(
                header["kind"],
                section["x"],
                header_y,
                width,
                lines,
                _label(lines),
                sex=(person or {}).get("sex", ""),
                href=(person or {}).get("href", ""),
            )
            nodes.append(header_node)
            if section["connect"]:
                root_targets.append((_cx(header_node), header_node["y"]))
        header_targets: list[tuple[float, float]] = []
        for group in section["groups"]:
            card_node = None
            if group["card"] and group["anchor"] in by_id:
                person = by_id[group["anchor"]]
                width = group["right"] - group["x"]
                lines = _ancestor_lines(person["name"], width)
                card_node = _box(
                    "branch",
                    group["x"],
                    card_y,
                    width,
                    lines,
                    _label(lines),
                    sex=person.get("sex", ""),
                    href=person.get("href", ""),
                )
                nodes.append(card_node)
                if group["from_root"]:
                    root_targets.append((_cx(card_node), card_node["y"]))
                elif group["edge"] and header_node is not None:
                    header_targets.append((_cx(card_node), card_node["y"]))
            cluster_targets: list[tuple[float, float]] = []
            for cluster in group["clusters"]:
                lines = _cluster_lines(cluster["label"], cluster["size"])
                node = _box("cluster", cluster["x"], cluster_y, COL_W, lines, _label(lines))
                nodes.append(node)
                if group["edge"] and not group["card"] and group["from_root"]:
                    root_targets.append((_cx(node), node["y"]))
                elif group["edge"] and not group["card"] and header_node is not None:
                    header_targets.append((_cx(node), node["y"]))
                elif group["edge"] and group["card"]:
                    cluster_targets.append((_cx(node), node["y"]))
            if card_node is not None and cluster_targets:
                _link_down(paths, _cx(card_node), card_node["y"] + card_node["h"], cluster_targets)
        if header_node is not None and header_targets:
            _link_down(paths, _cx(header_node), header_node["y"] + header_node["h"], header_targets)
    _root_bus(paths, _cx(root), root["y"] + root["h"], root_targets)
    width = max(node["x"] + node["w"] for node in nodes) + PAD
    height = max(node["y"] + node["h"] for node in nodes) + PAD
    return {"width": width, "height": height, "nodes": nodes, "paths": paths, "branched": True}


def _load_links(db: Session, space_id: int, kits: list[str]) -> dict[str, int]:
    if not kits:
        return {}
    pairs = db.execute(
        select(GedMatchHit.match_kit, GedMatchHit.person_id).where(
            GedMatchHit.space_id == space_id,
            GedMatchHit.match_kit.in_(kits),
            GedMatchHit.person_id.is_not(None),
        )
    ).all()
    return agreed_links([(kit, person_id) for kit, person_id in pairs if person_id is not None])


def _load_tree(db: Session, space_id: int, *, link_people: bool) -> tuple[list[dict], list[dict]]:
    people_rows = list(
        db.scalars(
            select(Person).join(Tree, Tree.id == Person.tree_id).where(Tree.space_id == space_id).order_by(Person.id)
        )
    )
    families = list(
        db.scalars(
            select(Family).join(Tree, Tree.id == Family.tree_id).where(Tree.space_id == space_id).order_by(Family.id)
        )
    )
    children: dict[int, list[int]] = {family.id: [] for family in families}
    if families:
        rows = db.execute(
            select(family_child.c.family_id, family_child.c.person_id)
            .where(family_child.c.family_id.in_([family.id for family in families]))
            .order_by(family_child.c.position, family_child.c.person_id)
        ).all()
        for family_id, person_id in rows:
            children[family_id].append(person_id)
    people = [
        {
            "id": person.id,
            "name": person.full_name,
            "sex": person.sex if person.sex in {"M", "F"} else "",
            "href": f"/s/{space_id}/people/{person.id}" if link_people else "",
        }
        for person in people_rows
    ]
    payload = [
        {
            "husband_id": family.husband_id,
            "wife_id": family.wife_id,
            "child_ids": children.get(family.id, []),
        }
        for family in families
    ]
    return people, payload


def _focus_id(people: list[dict], families: list[dict], focus_name: str) -> int | None:
    key = person_key(focus_name or "")
    if not key:
        return None
    exact = [person for person in people if person_key(person["name"]) == key]
    if not exact:
        return None
    if len(exact) == 1:
        return exact[0]["id"]
    parents: dict[int, int] = {}
    for family in families:
        pair = [person_id for person_id in (family.get("husband_id"), family.get("wife_id")) if person_id]
        for child_id in family.get("child_ids") or []:
            parents[child_id] = parents.get(child_id, 0) + len(pair)
    exact.sort(key=lambda person: (-parents.get(person["id"], 0), person["id"]))
    return exact[0]["id"]


def _graph(people: list[dict], families: list[dict]):
    by_id = {person["id"]: person for person in people}
    parents: dict[int, list[int]] = {person_id: [] for person_id in by_id}
    spouses: dict[int, list[int]] = {person_id: [] for person_id in by_id}
    for family in families:
        pair = [person_id for person_id in (family.get("husband_id"), family.get("wife_id")) if person_id in by_id]
        if len(pair) == 2 and pair[0] != pair[1]:
            for person_id, other in (pair, pair[::-1]):
                if other not in spouses[person_id]:
                    spouses[person_id].append(other)
        for child_id in family.get("child_ids") or []:
            if child_id not in by_id:
                continue
            for person_id in pair:
                if person_id not in parents[child_id]:
                    parents[child_id].append(person_id)
    return by_id, parents, spouses


def _ancestry(parents: dict[int, list[int]]):
    cache: dict[int, frozenset[int]] = {}

    def ancestors(person_id: int, trail: set[int] | None = None) -> frozenset[int]:
        cached = cache.get(person_id)
        if cached is not None:
            return cached
        if trail is None:
            trail = set()
        if person_id in trail:
            return frozenset({person_id})
        trail.add(person_id)
        found = {person_id}
        for parent_id in parents.get(person_id, []):
            found.update(ancestors(parent_id, trail))
        trail.discard(person_id)
        cache[person_id] = frozenset(found)
        return cache[person_id]

    depths = {person_id: 0 for person_id in parents}
    for _ in parents:
        changed = False
        for person_id, folks in parents.items():
            known = [parent_id for parent_id in folks if parent_id in depths]
            if not known:
                continue
            value = 1 + max(depths[parent_id] for parent_id in known)
            if depths[person_id] != value:
                depths[person_id] = value
                changed = True
        if not changed:
            break
    return ancestors, depths


def _sides(focus, by_id, parents, spouses, ancestors):
    if focus is None or len(parents.get(focus) or []) < 2:
        return None, None, set(), set()
    folks = parents[focus]
    father = next((person_id for person_id in folks if by_id[person_id].get("sex") == "M"), folks[0])
    mother = next(person_id for person_id in folks if person_id != father)
    paternal = _grow(set(ancestors(father)), set(ancestors(mother)), focus, by_id, parents, spouses)
    maternal = _grow(set(ancestors(mother)), set(ancestors(father)), focus, by_id, parents, spouses)
    paternal.discard(focus)
    maternal.discard(focus)
    return father, mother, paternal, maternal


def _grow(blood: set[int], other: set[int], focus: int, by_id, parents, spouses) -> set[int]:
    side = set(blood)
    changed = True
    while changed:
        changed = False
        for person_id in by_id:
            if person_id == focus or person_id in side or person_id in other:
                continue
            known = parents.get(person_id) or []
            on_side = any(parent_id in side for parent_id in known)
            on_other = any(parent_id in other for parent_id in known)
            if known and on_side and not on_other:
                side.add(person_id)
                changed = True
        for person_id in list(side):
            for spouse_id in spouses.get(person_id, []):
                if spouse_id != focus and spouse_id not in side and spouse_id not in other:
                    side.add(spouse_id)
                    changed = True
            for parent_id in parents.get(person_id, []):
                if parent_id not in side and parent_id not in other:
                    side.add(parent_id)
                    changed = True
    return side


def _visible_name(name: str) -> str:
    text = " ".join((name or "").split())
    if not text or "@" in text:
        return ""
    return text


def _root_lines(root_name: str) -> list[tuple]:
    lines = [("tree-strong", 16, "Тестируемый", "Тестируемый")]
    visible = _visible_name(root_name)
    if visible:
        lines.append(("tree-meta", 14, _clip(visible, 22), visible))
    lines.append(("tree-note", 13, "корень схемы, не имя предка", "корень схемы, не имя предка"))
    return lines


def _header_lines(header: dict, by_id: dict[int, dict], width: float) -> list[tuple]:
    title = header["title"]
    if header["kind"] == "twig":
        lines = [("tree-strong", 16, title, title)]
    else:
        lines = [("tree-accent", 14, title, title)]
    person = by_id.get(header["person_id"]) if header.get("person_id") else None
    visible = _visible_name(person["name"]) if person is not None else ""
    if visible:
        lines.append(("tree-strong", 16, _clip(visible, _limit(width)), visible))
    return lines


def _ancestor_lines(name: str, width: float) -> list[tuple]:
    visible = _visible_name(name)
    lines = []
    if visible:
        lines.append(("tree-strong", 16, _clip(visible, _limit(width)), visible))
    lines.append(("tree-note", 13, "общий предок", "общий предок"))
    return lines


def _cluster_lines(label: str, size: int) -> list[tuple]:
    title = f"Кластер {label}"
    phrase = _kit_phrase(size)
    return [
        ("tree-cluster", 15, title, title),
        ("tree-meta", 13, phrase, phrase),
    ]


def _kit_phrase(count: int) -> str:
    teen = abs(count) % 100
    last = teen % 10
    if 11 <= teen <= 14:
        word = "наборов"
    elif last == 1:
        word = "набор"
    elif last in {2, 3, 4}:
        word = "набора"
    else:
        word = "наборов"
    return f"{count} {word}"


def _label_key(label: str) -> tuple:
    return (0, int(label)) if str(label).isdigit() else (1, str(label))


def _limit(width: float) -> int:
    return max(12, int((width - 16) / 7))


def _block_height(lines: list[tuple]) -> float:
    return 6 + sum(line[1] for line in lines) + 6


def _label(lines: list[tuple]) -> str:
    return ", ".join(line[3] for line in lines[:2])


def _href(by_id: dict[int, dict], person_id: int | None) -> str:
    if person_id is None:
        return ""
    return (by_id.get(person_id) or {}).get("href") or ""


def _box(kind: str, x: float, y: float, w: float, lines: list[tuple], label: str, sex: str = "", href: str = "") -> dict:
    node = _node(kind, x, y, w, lines, label)
    node["sex"] = sex if sex in {"M", "F"} else ""
    node["href"] = href or ""
    return node


def _cx(node: dict) -> float:
    return node["x"] + node["w"] / 2


def _root_bus(paths: list[dict], parent_cx: float, parent_bottom: float, targets: list[tuple[float, float]]) -> None:
    if not targets:
        return
    bus_y = parent_bottom + BUS_DROP
    centers = [cx for cx, _top in targets]
    left = min([parent_cx, *centers])
    right = max([parent_cx, *centers])
    paths.append({"kind": "bus", "d": f"M {parent_cx:g} {parent_bottom:g} V {bus_y:g}"})
    if right != left:
        paths.append({"kind": "bus", "d": f"M {left:g} {bus_y:g} H {right:g}"})
    for cx, top in targets:
        paths.append({"kind": "link", "d": f"M {cx:g} {bus_y:g} V {top:g}"})


def _link_down(paths: list[dict], parent_cx: float, parent_bottom: float, targets: list[tuple[float, float]]) -> None:
    if not targets:
        return
    bus_y = parent_bottom + 14
    centers = [cx for cx, _top in targets]
    left = min([parent_cx, *centers])
    right = max([parent_cx, *centers])
    paths.append({"kind": "link", "d": f"M {parent_cx:g} {parent_bottom:g} V {bus_y:g}"})
    if right != left:
        paths.append({"kind": "bus", "d": f"M {left:g} {bus_y:g} H {right:g}"})
    for cx, top in targets:
        paths.append({"kind": "link", "d": f"M {cx:g} {bus_y:g} V {top:g}"})
