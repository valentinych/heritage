from __future__ import annotations

PAD = 20
CARD_W = 168
GUTTER = 76
V_GAP = 16
COUPLE_GAP = 8
BRANCH_GAP = 48
RAIL_GAP = 18
LANE_GAP = 20


def layout_pedigree(people: list[dict], families: list[dict]) -> dict | None:
    """Children on the left, older ancestors on the right.

    Each blood branch is one vertical block, walked from its oldest parentless
    ancestors toward younger descendants. A marriage that joins two branches
    stacks those blocks on top of each other so both parent links stay short.
    """
    if not people:
        return None
    by_id = {person["id"]: person for person in people}
    parents: dict[int, list[int]] = {person["id"]: [] for person in people}
    spouses: dict[int, list[int]] = {person["id"]: [] for person in people}
    for family in families:
        pair = [
            person_id
            for person_id in (family.get("husband_id"), family.get("wife_id"))
            if person_id in by_id
        ]
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

    generation = _generations(list(by_id), parents, spouses)
    _align_spouses(spouses, generation)
    max_gen = max(generation.values())
    units, unit_of = _units(by_id, spouses, generation)
    parent_units = {id(unit): [] for unit in units}
    child_units: dict[int, list[dict]] = {id(unit): [] for unit in units}
    for unit in units:
        found = []
        for person_id in unit["ids"]:
            for parent_id in parents[person_id]:
                parent_unit = unit_of.get(parent_id)
                if parent_unit is not None and parent_unit is not unit and parent_unit not in found:
                    found.append(parent_unit)
        parent_units[id(unit)] = found
        for parent_unit in found:
            if unit not in child_units[id(parent_unit)]:
                child_units[id(parent_unit)].append(unit)

    def side_size(start: dict, barrier: dict) -> int:
        seen = {id(barrier)}
        stack = [start]
        count = 0
        while stack:
            current = stack.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            count += 1
            stack.extend(parent_units[id(current)])
            stack.extend(child_units[id(current)])
        return count

    def parent_rank(parent: dict, couple: dict) -> tuple:
        dist = couple["gen"] - parent["gen"]
        adjacent = dist == 1
        return (not adjacent, dist if dist > 0 else 99, -side_size(parent, couple), parent["ids"][0])

    kids_of: dict[int, list[dict]] = {id(unit): [] for unit in units}
    ordered_parents: dict[int, list[dict]] = {}
    for unit in units:
        found = sorted(parent_units[id(unit)], key=lambda item: parent_rank(item, unit))
        ordered_parents[id(unit)] = found
        if found:
            kids_of[id(found[0])].append(unit)
    for kids in kids_of.values():
        kids.sort(key=lambda item: item["ids"][0])

    def root_unit(unit: dict) -> dict:
        seen: set[int] = set()
        while ordered_parents[id(unit)] and id(unit) not in seen:
            seen.add(id(unit))
            unit = ordered_parents[id(unit)][0]
        return unit

    home: dict[int, int] = {}
    members: dict[int, list[dict]] = {}
    for unit in units:
        root = root_unit(unit)
        home[id(unit)] = id(root)
        members.setdefault(id(root), []).append(unit)
    roots = {id(root_unit(unit)): root_unit(unit) for unit in units}

    pulls: list[dict] = []
    for unit in units:
        found = ordered_parents[id(unit)]
        for extra in found[1:]:
            if home[id(extra)] == home[id(unit)]:
                continue
            child_id = next(
                (
                    person_id
                    for person_id in unit["ids"]
                    if any(unit_of.get(parent_id) is extra for parent_id in parents[person_id])
                ),
                unit["ids"][0],
            )
            pulls.append(
                {
                    "target": home[id(unit)],
                    "extra": home[id(extra)],
                    "couple": unit,
                    "anchor": extra,
                    "child_id": child_id,
                }
            )

    pulled = {pull["extra"] for pull in pulls}
    by_target: dict[int, list[dict]] = {}
    for pull in pulls:
        by_target.setdefault(pull["target"], []).append(pull)
    for group in by_target.values():
        group.sort(key=lambda item: (item["couple"]["gen"], item["anchor"]["ids"][0]))

    def layout_component(home_id: int, anchor: dict | None) -> list[dict]:
        cursor = [0.0]
        if anchor is not None:
            _order_toward(roots[home_id], anchor, kids_of)
        _place_tree(roots[home_id], kids_of, cursor, "top" if anchor is not None else "center")
        block = list(members[home_id])
        _shift_members(block, -min(unit["y"] for unit in block))
        occupied = list(block)
        for pull in by_target.get(home_id, []):
            extra_block = layout_component(pull["extra"], pull["anchor"])
            _pack_near(
                extra_block,
                pull["anchor"],
                pull["couple"],
                pull["child_id"],
                occupied,
                by_id,
                parents,
                unit_of,
                parent_units,
                child_units,
            )
            occupied.extend(extra_block)
            block.extend(extra_block)
        members[home_id] = block
        return block

    cursor_y = 0.0
    placed: list[dict] = []
    for home_id in sorted((key for key in members if key not in pulled), key=lambda key: roots[key]["ids"][0]):
        block = layout_component(home_id, None)
        top = min(unit["y"] for unit in block)
        _shift_members(block, cursor_y - top)
        cursor_y = max(unit["y"] + unit["h"] for unit in block) + BRANCH_GAP
        placed.extend(block)
    missing = [unit for unit in units if unit not in placed]
    if missing:
        _shift_members(missing, cursor_y - min(unit["y"] for unit in missing))

    _normalize(units)
    _rebalance_focus(by_id, parents, spouses, units)
    _normalize(units)

    nodes = []
    placed_nodes: dict[int, dict] = {}
    step = CARD_W + GUTTER
    for unit in units:
        cursor_card = unit["y"]
        for index, person_id in enumerate(unit["ids"]):
            if index:
                cursor_card += COUPLE_GAP
            person = by_id[person_id]
            height = _card_height(person)
            lines = [("tree-strong", 16, _clip(person["name"], 22), person["name"])]
            label = person["name"]
            if person.get("detail"):
                lines.append(("tree-meta", 14, person["detail"], person["detail"]))
                label = f"{person['name']}, {person['detail']}"
            sex = person.get("sex") or ""
            node = {
                "id": person_id,
                "kind": "person",
                "sex": sex if sex in {"M", "F"} else "",
                "x": PAD + (max_gen - generation[person_id]) * step,
                "y": cursor_card,
                "w": CARD_W,
                "h": height,
                "label": label,
                "href": person.get("href") or "",
                "lines": [
                    {"class": css, "dy": line_h, "text": text, "full": full}
                    for css, line_h, text, full in lines
                ],
            }
            nodes.append(node)
            placed_nodes[person_id] = node
            cursor_card += height

    paths = []
    drawn_pairs: set[tuple[int, int]] = set()
    for person_id, spouse_ids in spouses.items():
        for spouse_id in spouse_ids:
            key = (min(person_id, spouse_id), max(person_id, spouse_id))
            if key in drawn_pairs or person_id not in placed_nodes or spouse_id not in placed_nodes:
                continue
            drawn_pairs.add(key)
            path = _spouse_path(placed_nodes[person_id], placed_nodes[spouse_id], nodes)
            if _span(path) <= 360:
                paths.append({"kind": "spouse", "d": path})
    buses = _family_buses(parents, placed_nodes)
    link_paths: list[dict] = []
    for child_id, parent_ids in parents.items():
        if child_id not in placed_nodes:
            continue
        family = frozenset(parent_id for parent_id in parent_ids if parent_id in placed_nodes)
        for parent_id in parent_ids:
            if parent_id not in placed_nodes:
                continue
            child = placed_nodes[child_id]
            parent = placed_nodes[parent_id]
            near, far = _buses_for_link(child, parent, family, buses)
            drawn = _link_path(parent, child, nodes, near, far)
            link_paths.append({"family": family, "d": drawn, "segments": _path_segments(drawn)})
    colors = _family_colors(link_paths)
    for item in link_paths:
        paths.append({"kind": "link", "d": item["d"], "css": f"tree-link-{colors[item['family']]}"})

    for node in nodes:
        node.pop("id", None)
    width = max(node["x"] + node["w"] for node in nodes) + PAD
    height = max(node["y"] + node["h"] for node in nodes) + PAD
    return {"width": width, "height": height, "nodes": nodes, "paths": paths, "branched": False}


def _order_toward(root: dict, anchor: dict, kids_of: dict[int, list[dict]]) -> None:
    def reaches(unit: dict) -> bool:
        if unit is anchor:
            return True
        return any(reaches(kid) for kid in kids_of[id(unit)])

    def walk(unit: dict) -> None:
        kids = kids_of[id(unit)]
        kids.sort(key=lambda kid: (not reaches(kid), kid["ids"][0]))
        for kid in kids:
            walk(kid)

    walk(root)


def _place_tree(unit: dict, kids_of: dict[int, list[dict]], cursor: list[float], align: str) -> None:
    kids = kids_of[id(unit)]
    if not kids:
        unit["y"] = cursor[0]
        cursor[0] += unit["h"] + V_GAP
        return
    for kid in kids:
        _place_tree(kid, kids_of, cursor, align)
    top = kids[0]["y"]
    bottom = kids[-1]["y"] + kids[-1]["h"]
    if align == "top":
        ideal = top
    else:
        ideal = (top + bottom - unit["h"]) / 2
        if ideal < top:
            for kid in kids:
                _shift(kid, kids_of, top - ideal)
            top = kids[0]["y"]
            bottom = kids[-1]["y"] + kids[-1]["h"]
            ideal = (top + bottom - unit["h"]) / 2
    unit["y"] = ideal
    cursor[0] = max(cursor[0], bottom + V_GAP, ideal + unit["h"] + V_GAP)


def _shift_members(units: list[dict], delta: float) -> None:
    if delta == 0:
        return
    for unit in units:
        unit["y"] += delta


def _pack_near(
    block: list[dict],
    anchor: dict,
    couple: dict,
    child_id: int,
    occupied: list[dict],
    by_id: dict[int, dict],
    parents: dict[int, list[int]],
    unit_of: dict[int, dict],
    parent_units: dict[int, list[dict]],
    child_units: dict[int, list[dict]],
) -> None:
    """Seat a joined branch as one block beside the marriage.

    Cards keep the gaps their own tree needs. The block moves up or down
    together instead of filling holes between other families.
    """
    child_top = _person_top(couple, child_id, by_id)
    anchor_person = _anchor_person(anchor, couple, parents)
    anchor_top = _person_top(anchor, anchor_person, by_id)
    _shift_members(block, child_top - anchor_top)
    _clear_block(block, [unit for unit in occupied if unit is not couple])


def _clear_block(block: list[dict], occupied: list[dict]) -> None:
    """Move a whole branch out of other families, keeping its inner gaps."""
    others = [unit for unit in occupied if unit not in block]
    if not others or not block:
        return
    for _ in range(len(others) + 2):
        top = min(unit["y"] for unit in block)
        bottom = max(unit["y"] + unit["h"] for unit in block)
        hits = [
            unit
            for unit in others
            if unit["y"] < bottom + BRANCH_GAP and top < unit["y"] + unit["h"] + BRANCH_GAP
            and any(unit["gen"] == member["gen"] for member in block)
        ]
        if not hits:
            return
        height = bottom - top
        down = max(unit["y"] + unit["h"] for unit in hits) + BRANCH_GAP - top
        up = min(unit["y"] for unit in hits) - BRANCH_GAP - height - top
        _shift_members(block, down if abs(down) <= abs(up) else up)


def _anchor_person(anchor: dict, couple: dict, parents: dict[int, list[int]]) -> int:
    for person_id in anchor["ids"]:
        if any(person_id in parents[child_id] for child_id in couple["ids"]):
            return person_id
    return anchor["ids"][0]


def _person_top(unit: dict, person_id: int, by_id: dict[int, dict]) -> float:
    return _person_top_at(unit, person_id, by_id, {id(unit): unit["y"]})


def _person_top_at(unit: dict, person_id: int, by_id: dict[int, dict], tops: dict[int, float]) -> float:
    cursor = tops[id(unit)]
    for index, member_id in enumerate(unit["ids"]):
        if index:
            cursor += COUPLE_GAP
        if member_id == person_id:
            return cursor
        cursor += _card_height(by_id[member_id])
    return tops[id(unit)]


def _units(by_id: dict[int, dict], spouses: dict[int, list[int]], generation: dict[int, int]):
    units = []
    unit_of: dict[int, dict] = {}
    paired: set[int] = set()
    for person_id in sorted(by_id, key=lambda item: (generation[item], item)):
        if person_id in paired:
            continue
        group = [person_id]
        paired.add(person_id)
        for spouse_id in sorted(spouses[person_id]):
            if spouse_id not in paired and generation[spouse_id] == generation[person_id]:
                group.append(spouse_id)
                paired.add(spouse_id)
        height = 0.0
        for index, member_id in enumerate(group):
            if index:
                height += COUPLE_GAP
            height += _card_height(by_id[member_id])
        unit = {"ids": group, "gen": generation[person_id], "y": 0.0, "h": height}
        units.append(unit)
        for member_id in group:
            unit_of[member_id] = unit
    return units, unit_of


def _shift(unit: dict, kids_of: dict[int, list[dict]], delta: float) -> None:
    if delta == 0:
        return
    unit["y"] += delta
    for kid in kids_of[id(unit)]:
        _shift(kid, kids_of, delta)


def _normalize(units: list[dict]) -> None:
    top = min(unit["y"] for unit in units)
    if top == PAD:
        return
    delta = PAD - top
    for unit in units:
        unit["y"] += delta


def _span(path: str) -> float:
    parts = path.split()
    index = 0
    y = None
    longest = 0.0
    while index < len(parts):
        cmd = parts[index]
        if cmd == "M":
            y = float(parts[index + 2])
            index += 3
        elif cmd == "H":
            index += 2
        elif cmd == "V":
            nxt = float(parts[index + 1])
            longest = max(longest, abs(nxt - y))
            y = nxt
            index += 2
        else:
            break
    return longest


LINK_COLORS = 6


def _path_segments(path: str) -> list[tuple]:
    parts = path.split()
    index = 0
    x = y = None
    segments = []
    while index < len(parts):
        cmd = parts[index]
        if cmd == "M":
            x = float(parts[index + 1])
            y = float(parts[index + 2])
            index += 3
        elif cmd == "H":
            nxt = float(parts[index + 1])
            if abs(nxt - x) > 0.5:
                segments.append(("H", min(x, nxt), max(x, nxt), y))
            x = nxt
            index += 2
        elif cmd == "V":
            nxt = float(parts[index + 1])
            if abs(nxt - y) > 0.5:
                segments.append(("V", x, min(y, nxt), max(y, nxt)))
            y = nxt
            index += 2
        else:
            break
    return segments


def _ranges_overlap(a0: float, a1: float, b0: float, b1: float) -> bool:
    return min(a1, b1) - max(a0, b0) > 1


def _segments_meet(left: tuple, right: tuple) -> bool:
    if left[0] == "V" and right[0] == "V":
        return abs(left[1] - right[1]) < GUTTER and _ranges_overlap(left[2], left[3], right[2], right[3])
    if left[0] == "H" and right[0] == "H":
        return abs(left[3] - right[3]) < 1 and _ranges_overlap(left[1], left[2], right[1], right[2])
    horizontal, vertical = (left, right) if left[0] == "H" else (right, left)
    return horizontal[1] - 0.5 < vertical[1] < horizontal[2] + 0.5 and vertical[2] - 0.5 < horizontal[3] < vertical[3] + 0.5


def _families_cross(left: list[tuple], right: list[tuple]) -> bool:
    return any(_segments_meet(a, b) for a in left for b in right)


def _family_colors(link_paths: list[dict]) -> dict[frozenset[int], int]:
    grouped: dict[frozenset[int], list[tuple]] = {}
    for item in link_paths:
        grouped.setdefault(item["family"], []).extend(item["segments"])
    def top(segment: tuple) -> float:
        return segment[3] if segment[0] == "H" else segment[2]

    order = sorted(grouped, key=lambda family: (min(top(segment) for segment in grouped[family]), min(family)))
    colors: dict[frozenset[int], int] = {}
    for family in order:
        used = {
            colors[other]
            for other in colors
            if _families_cross(grouped[family], grouped[other])
        }
        colors[family] = next((index for index in range(LINK_COLORS) if index not in used), 0)
    return colors


def _family_buses(parents: dict[int, list[int]], placed: dict[int, dict]) -> dict[tuple[frozenset[int], float], float]:
    """One gutter x per nuclear family, so neighbouring couples do not share a rail."""
    spans: dict[tuple[frozenset[int], float], list[float]] = {}
    for child_id, parent_ids in parents.items():
        if child_id not in placed:
            continue
        family = frozenset(parent_id for parent_id in parent_ids if parent_id in placed)
        if not family:
            continue
        child = placed[child_id]
        child_y = child["y"] + child["h"] / 2
        for parent_id in family:
            parent = placed[parent_id]
            parent_y = parent["y"] + parent["h"] / 2
            for gutter in _gutter_columns(child["x"], parent["x"]):
                spans.setdefault((family, gutter), []).extend((child_y, parent_y))
    by_gutter: dict[float, list[tuple[frozenset[int], float, float]]] = {}
    for (family, gutter), ys in spans.items():
        by_gutter.setdefault(gutter, []).append((family, min(ys), max(ys)))
    buses: dict[tuple[frozenset[int], float], float] = {}
    for gutter, groups in by_gutter.items():
        groups.sort(key=lambda item: (item[1], item[2], min(item[0])))
        lane_ends: list[float] = []
        lanes: list[int] = []
        for _, y0, y1 in groups:
            chosen = None
            for index, end in enumerate(lane_ends):
                if y0 >= end + RAIL_GAP:
                    chosen = index
                    lane_ends[index] = y1
                    break
            if chosen is None:
                chosen = len(lane_ends)
                lane_ends.append(y1)
            lanes.append(chosen)
        offsets = _lane_offsets(len(lane_ends))
        for (family, _, _), lane in zip(groups, lanes):
            buses[(family, gutter)] = gutter + CARD_W + offsets[lane]
    return buses


def _gutter_columns(left_x: float, right_x: float) -> list[float]:
    if right_x < left_x:
        left_x, right_x = right_x, left_x
    step = CARD_W + GUTTER
    columns = []
    column = left_x
    while column + step < right_x - 1:
        columns.append(column)
        column += step
    columns.append(right_x - step)
    return columns


def _lane_offsets(count: int) -> list[float]:
    if count <= 1:
        return [GUTTER / 2]
    room = GUTTER - 16
    need = (count - 1) * LANE_GAP
    if need <= room:
        start = (GUTTER - need) / 2
        return [start + index * LANE_GAP for index in range(count)]
    step = room / (count - 1)
    return [8 + index * step for index in range(count)]


def _buses_for_link(child: dict, parent: dict, family: frozenset[int], buses: dict) -> tuple[float, float]:
    columns = _gutter_columns(child["x"], parent["x"])
    near = buses[(family, columns[0])]
    far = buses[(family, columns[-1])]
    if parent["x"] >= child["x"]:
        return near, far
    return far, near


def _link_path(parent: dict, child: dict, nodes: list[dict], bus_near: float, bus_far: float) -> str:
    child_y = child["y"] + child["h"] / 2
    parent_y = parent["y"] + parent["h"] / 2
    if parent["x"] >= child["x"]:
        start_x = child["x"] + child["w"]
        end_x = parent["x"]
        start_y = child_y
        end_y = parent_y
        first_bus, second_bus = bus_near, bus_far
    else:
        start_x = parent["x"] + parent["w"]
        end_x = child["x"]
        start_y = parent_y
        end_y = child_y
        first_bus, second_bus = bus_far, bus_near
    if abs(end_x - start_x - GUTTER) < 1:
        return f"M {start_x:g} {start_y:g} H {first_bus:g} V {end_y:g} H {end_x:g}"
    channel = _clear_y(start_y, end_y, first_bus, second_bus, nodes)
    return (
        f"M {start_x:g} {start_y:g} H {first_bus:g} V {channel:g} "
        f"H {second_bus:g} V {end_y:g} H {end_x:g}"
    )


def _clear_y(y0: float, y1: float, x0: float, x1: float, nodes: list[dict]) -> float:
    def blocked(y: float) -> bool:
        for node in nodes:
            if node["x"] + node["w"] <= x0 + 0.5 or node["x"] >= x1 - 0.5:
                continue
            if node["y"] + 0.8 < y < node["y"] + node["h"] - 0.8:
                return True
        return False

    if not blocked(y1):
        return y1
    if not blocked(y0):
        return y0
    candidates = []
    for node in nodes:
        if node["x"] + node["w"] <= min(x0, x1) + 0.5 or node["x"] >= max(x0, x1) - 0.5:
            continue
        candidates.append(node["y"] - 6)
        candidates.append(node["y"] + node["h"] + 6)
    mid = (y0 + y1) / 2
    candidates.sort(key=lambda value: abs(value - mid))
    for value in candidates:
        if not blocked(value):
            return value
    return mid


def _spouse_path(left: dict, right: dict, nodes: list[dict]) -> str:
    if abs(left["x"] - right["x"]) < 1:
        top, bottom = (left, right) if left["y"] <= right["y"] else (right, left)
        if _gap_clear(top, bottom, nodes):
            center = top["x"] + top["w"] / 2
            return f"M {center:g} {top['y'] + top['h']:g} V {bottom['y']:g}"
        bus = top["x"] + top["w"] + 14
        return (
            f"M {top['x'] + top['w']:g} {top['y'] + top['h'] / 2:g} H {bus:g} "
            f"V {bottom['y'] + bottom['h'] / 2:g} H {bottom['x'] + bottom['w']:g}"
        )
    first, second = (left, right) if left["x"] <= right["x"] else (right, left)
    bus = first["x"] + first["w"] + GUTTER / 2
    return (
        f"M {first['x'] + first['w']:g} {first['y'] + first['h'] / 2:g} H {bus:g} "
        f"V {second['y'] + second['h'] / 2:g} H {second['x']:g}"
    )


def _gap_clear(top: dict, bottom: dict, nodes: list[dict]) -> bool:
    y0 = top["y"] + top["h"]
    y1 = bottom["y"]
    if y1 < y0:
        return False
    for node in nodes:
        if node is top or node is bottom:
            continue
        if abs(node["x"] - top["x"]) > 1:
            continue
        if node["y"] + node["h"] > y0 + 0.5 and node["y"] < y1 - 0.5:
            return False
    return True


def _generations(ids: list[int], parents: dict[int, list[int]], spouses: dict[int, list[int]]) -> dict[int, int]:
    generation = {person_id: 0 for person_id in ids}
    for _ in range(len(ids) + 1):
        changed = False
        for person_id in ids:
            known = [generation[parent_id] for parent_id in parents[person_id] if parent_id in generation]
            if parents[person_id] and len(known) == len(parents[person_id]):
                value = max(known) + 1
                if generation[person_id] != value:
                    generation[person_id] = value
                    changed = True
        for person_id in ids:
            if parents[person_id]:
                continue
            known = [generation[spouse_id] for spouse_id in spouses[person_id] if spouse_id in generation]
            if known and generation[person_id] != max(known):
                generation[person_id] = max(known)
                changed = True
        if not changed:
            break
    return generation


def _open_columns(units: list[dict]) -> None:
    """Push cards in one column apart. Leave the gap instead of stacking them."""
    by_gen: dict[int, list[dict]] = {}
    for unit in units:
        by_gen.setdefault(unit["gen"], []).append(unit)
    for group in by_gen.values():
        ordered = sorted(group, key=lambda unit: (unit["y"], unit["ids"][0]))
        for previous, unit in zip(ordered, ordered[1:]):
            floor = previous["y"] + previous["h"] + V_GAP
            if unit["y"] < floor:
                unit["y"] = floor


def _align_spouses(spouses, generation) -> None:
    """Put a married couple in the column closer to their children."""
    ids = list(generation)
    for _ in range(len(ids) + 1):
        changed = False
        for person_id in ids:
            for spouse_id in spouses[person_id]:
                if spouse_id not in generation:
                    continue
                shared = max(generation[person_id], generation[spouse_id])
                if generation[person_id] != shared:
                    generation[person_id] = shared
                    changed = True
        if not changed:
            break


def _rebalance_focus(by_id, parents, spouses, units) -> None:
    """Keep the short paternal line above Ruslan and the long maternal line below him."""
    focus = next((person["id"] for person in by_id.values() if person["name"].startswith("Руслан Огородник")), None)
    if focus is None or len(parents[focus]) < 2:
        return
    father = next((person_id for person_id in parents[focus] if by_id[person_id].get("sex") == "M"), parents[focus][0])
    mother = next(person_id for person_id in parents[focus] if person_id != father)

    def ancestors(start: int) -> set[int]:
        seen: set[int] = set()
        stack = [start]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(parents[current])
        return seen

    def grow(blood: set[int], other: set[int]) -> set[int]:
        side = set(blood)
        changed = True
        while changed:
            changed = False
            for person_id in by_id:
                if person_id == focus or person_id in side or person_id in other:
                    continue
                known = parents[person_id]
                if known and any(parent_id in side for parent_id in known) and not any(parent_id in other for parent_id in known):
                    side.add(person_id)
                    changed = True
            for person_id in list(side):
                for spouse_id in spouses[person_id]:
                    if spouse_id != focus and spouse_id not in side and spouse_id not in other:
                        side.add(spouse_id)
                        changed = True
                for parent_id in parents[person_id]:
                    if parent_id not in side and parent_id not in other:
                        side.add(parent_id)
                        changed = True
        return side

    paternal = grow(ancestors(father), ancestors(mother))
    maternal = grow(ancestors(mother), ancestors(father))
    groups = {"pat": [], "father": [], "focus": [], "mother": [], "mat": [], "other": []}
    for unit in units:
        members = set(unit["ids"])
        if focus in members:
            groups["focus"].append(unit)
        elif father in members and mother in members:
            groups["father"].append(unit)
        elif members & paternal and not members & maternal:
            groups["pat" if father not in members else "father"].append(unit)
        elif members & maternal and not members & paternal:
            groups["mat" if mother not in members else "mother"].append(unit)
        elif father in members:
            groups["father"].append(unit)
        elif mother in members:
            groups["mother"].append(unit)
        else:
            groups["other"].append(unit)

    def slide(group: list[dict], top: float) -> float:
        if not group:
            return top
        delta = top - min(unit["y"] for unit in group)
        for unit in group:
            unit["y"] += delta
        return max(unit["y"] + unit["h"] for unit in group)

    couple = groups["father"] or groups["mother"]
    if not couple:
        return
    couple_top = slide(groups["pat"], 0.0) + BRANCH_GAP if groups["pat"] else 0.0
    slide(couple, couple_top)
    below = couple[0]["y"] + couple[0]["h"] + BRANCH_GAP
    if groups["mother"] and groups["mother"] is not couple:
        below = slide(groups["mother"], below) + BRANCH_GAP
    below = slide(groups["mat"], below) + BRANCH_GAP
    pair = next((unit for unit in couple if father in unit["ids"] and mother in unit["ids"]), couple[0])
    if groups["focus"]:
        groups["focus"][0]["y"] = pair["y"] + (pair["h"] - groups["focus"][0]["h"]) / 2
    slide(groups["other"], below)
    _open_columns(units)


def _card_height(person: dict) -> float:
    return 42 if person.get("detail") else 28


def _clip(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"
