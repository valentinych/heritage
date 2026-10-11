from __future__ import annotations

from app.templating import format_cm

PAD = 20
COL_W = 258
GAP = 14
ROOT_W = 280
BUS_DROP = 22
BUS_GAP = 18
STACK_GAP = 8


def layout_cluster_tree(groups: list[dict], root_name: str) -> dict:
    """Top-down scheme: tester, then one column per cluster, people stacked underneath."""
    columns = []
    x = PAD
    cluster_top = PAD + _height([("tree-strong", 16), ("tree-meta", 14), ("tree-note", 13)]) + BUS_DROP + BUS_GAP
    for group in groups:
        members = group["members"]
        cluster_lines = [
            ("tree-cluster", 15, f"Кластер {group['label']}", f"Кластер {group['label']}"),
            ("tree-meta", 13, _people_count(len(members)), _people_count(len(members))),
        ]
        nodes = [_node("cluster", x, cluster_top, COL_W, cluster_lines, f"Кластер {group['label']}, {len(members)}")]
        y = nodes[0]["y"] + nodes[0]["h"] + STACK_GAP
        previous = nodes[0]
        for member in members:
            cm = format_cm(member.shared_cm) if member.shared_cm is not None else ""
            detail = f"{cm} cM" if cm and cm != "—" else ""
            lines = [("tree-strong", 16, _clip(member.name, 32), member.name)]
            label = member.name
            if detail:
                lines.append(("tree-meta", 14, detail, detail))
                label = f"{member.name}, {detail}"
            person = _node("person", x, y, COL_W, lines, label)
            nodes.append(person)
            y = person["y"] + person["h"] + STACK_GAP
            previous = person
        columns.append({"x": x, "nodes": nodes, "cx": x + COL_W / 2, "previous": previous})
        x += COL_W + GAP

    root_lines = [
        ("tree-strong", 16, "Тестируемый", "Тестируемый"),
        ("tree-meta", 14, _clip(root_name, 34), root_name),
        ("tree-note", 13, "корень схемы, не имя предка", "корень схемы, не имя предка"),
    ]
    root = _node("root", PAD, PAD, ROOT_W, root_lines, f"Тестируемый, {root_name}")
    root_cx = root["x"] + root["w"] / 2
    root_bottom = root["y"] + root["h"]
    bus_y = root_bottom + BUS_DROP

    paths = [{"kind": "bus", "d": f"M {root_cx:g} {root_bottom:g} V {bus_y:g}"}]
    if columns:
        centers = [column["cx"] for column in columns]
        left = min([root_cx, *centers])
        right = max([root_cx, *centers])
        if right != left:
            paths.append({"kind": "bus", "d": f"M {left:g} {bus_y:g} H {right:g}"})
        for column in columns:
            cluster = column["nodes"][0]
            paths.append({"kind": "link", "d": f"M {column['cx']:g} {bus_y:g} V {cluster['y']:g}"})
            bottom = cluster["y"] + cluster["h"]
            for person in column["nodes"][1:]:
                paths.append({"kind": "link", "d": f"M {column['cx']:g} {bottom:g} V {person['y']:g}"})
                bottom = person["y"] + person["h"]

    nodes = [root]
    for column in columns:
        nodes.extend(column["nodes"])
    width = max(node["x"] + node["w"] for node in nodes) + PAD
    height = max(node["y"] + node["h"] for node in nodes) + PAD
    return {"width": width, "height": height, "nodes": nodes, "paths": paths, "branched": False}


def layout_branches(root_name: str, branches: list[dict]) -> dict:
    """Tester, then an ancestor card per branch, then that branch's clusters and people."""
    root = _node(
        "root",
        PAD,
        PAD,
        ROOT_W,
        [
            ("tree-strong", 16, "Тестируемый", "Тестируемый"),
            ("tree-meta", 14, _clip(root_name, 34), root_name),
            ("tree-note", 13, "корень схемы, не имя предка", "корень схемы, не имя предка"),
        ],
        f"Тестируемый, {root_name}",
    )
    branch_h = max(_height([(line[0], line[1]) for line in branch["lines"]]) for branch in branches)
    branch_y = root["y"] + root["h"] + BUS_DROP + BUS_GAP
    column_y = branch_y + branch_h + 28
    nodes = [root]
    paths = []
    branch_cards = []
    x = PAD
    for branch in branches:
        columns = []
        start = x
        for column in branch["columns"]:
            header = _node("cluster", x, column_y, COL_W, column["lines"], _label_from_lines(column["lines"]))
            column_nodes = [header]
            y = header["y"] + header["h"] + STACK_GAP
            bottom = header["y"] + header["h"]
            for member in column["members"]:
                person = _person_node(member, x, y)
                column_nodes.append(person)
                paths.append({"kind": column["link"], "d": f"M {x + COL_W / 2:g} {bottom:g} V {person['y']:g}"})
                bottom = person["y"] + person["h"]
                y = bottom + STACK_GAP
            columns.append({"cx": x + COL_W / 2, "top": header["y"], "link": column["link"], "nodes": column_nodes})
            x += COL_W + GAP
        width = (x - GAP) - start
        card = _node(branch["kind"], start, branch_y, width, branch["lines"], _label_from_lines(branch["lines"]))
        card["h"] = branch_h
        branch_cards.append({"card": card, "cx": start + width / 2, "link": branch["link"], "columns": columns})
        nodes.append(card)
        for column in columns:
            nodes.extend(column["nodes"])
        _drop(
            paths,
            card["x"] + card["w"] / 2,
            card["y"] + card["h"],
            [(column["cx"], column["top"], column["link"]) for column in columns],
            branch["link"],
        )

    root_cx = root["x"] + root["w"] / 2
    root_bottom = root["y"] + root["h"]
    bus_y = root_bottom + BUS_DROP
    paths.insert(0, {"kind": "bus", "d": f"M {root_cx:g} {root_bottom:g} V {bus_y:g}"})
    if branch_cards:
        centers = [item["cx"] for item in branch_cards]
        left = min([root_cx, *centers])
        right = max([root_cx, *centers])
        if right != left:
            paths.insert(1, {"kind": "bus", "d": f"M {left:g} {bus_y:g} H {right:g}"})
        for item in branch_cards:
            paths.append({"kind": item["link"], "d": f"M {item['cx']:g} {bus_y:g} V {item['card']['y']:g}"})
    width = max(node["x"] + node["w"] for node in nodes) + PAD
    height = max(node["y"] + node["h"] for node in nodes) + PAD
    return {"width": width, "height": height, "nodes": nodes, "paths": paths, "branched": True}


def layout_self_sides(self_name: str, sides: list[dict]) -> dict:
    """Self at the start. Each side is one uploaded person's file, laid out by the single-file rules."""
    if len(sides) == 1:
        side = sides[0]
        if side.get("branches"):
            return layout_branches(self_name, side["branches"])
        return layout_cluster_tree(side["groups"], self_name)

    placed = []
    x = PAD
    for side in sides:
        if side.get("branches"):
            scheme = layout_branches(side["owner"], side["branches"])
        else:
            scheme = layout_cluster_tree(side["groups"], side["owner"])
        placed.append((scheme, x))
        x += scheme["width"] + GAP
    root = _node(
        "root",
        PAD,
        PAD,
        ROOT_W,
        [
            ("tree-strong", 16, "Вы", "Вы"),
            ("tree-meta", 14, _clip(self_name, 34), self_name),
            ("tree-note", 13, "корень схемы, не имя предка", "корень схемы, не имя предка"),
        ],
        f"Вы, {self_name}",
    )
    top = root["y"] + root["h"] + BUS_DROP + BUS_GAP
    nodes = [root]
    paths = []
    centers = []
    for scheme, origin_x in placed:
        dx = origin_x - PAD
        dy = top - PAD
        for node in scheme["nodes"]:
            moved = dict(node)
            moved["x"] = node["x"] + dx
            moved["y"] = node["y"] + dy
            nodes.append(moved)
        for path in scheme["paths"]:
            copied = {"kind": path["kind"], "d": _shift_d(path["d"], dx, dy)}
            if "css" in path:
                copied["css"] = path["css"]
            paths.append(copied)
        side_root = scheme["nodes"][0]
        centers.append(side_root["x"] + dx + side_root["w"] / 2)
    root_cx = root["x"] + root["w"] / 2
    root_bottom = root["y"] + root["h"]
    bus_y = root_bottom + BUS_DROP
    paths.insert(0, {"kind": "bus", "d": f"M {root_cx:g} {root_bottom:g} V {bus_y:g}"})
    if centers:
        left = min([root_cx, *centers])
        right = max([root_cx, *centers])
        if right != left:
            paths.insert(1, {"kind": "bus", "d": f"M {left:g} {bus_y:g} H {right:g}"})
        for center in centers:
            paths.append({"kind": "link", "d": f"M {center:g} {bus_y:g} V {top:g}"})
    width = max(node["x"] + node["w"] for node in nodes) + PAD
    height = max(node["y"] + node["h"] for node in nodes) + PAD
    return {"width": width, "height": height, "nodes": nodes, "paths": paths, "branched": True}


def _shift_d(path: str, dx: float, dy: float) -> str:
    parts = path.split()
    out = []
    index = 0
    while index < len(parts):
        cmd = parts[index]
        if cmd == "M":
            out.extend(["M", f"{float(parts[index + 1]) + dx:g}", f"{float(parts[index + 2]) + dy:g}"])
            index += 3
        elif cmd == "H":
            out.extend(["H", f"{float(parts[index + 1]) + dx:g}"])
            index += 2
        elif cmd == "V":
            out.extend(["V", f"{float(parts[index + 1]) + dy:g}"])
            index += 2
        else:
            out.append(cmd)
            index += 1
    return " ".join(out)


def _person_node(member, x: float, y: float) -> dict:
    cm = format_cm(member.shared_cm) if member.shared_cm is not None else ""
    detail = f"{cm} cM" if cm and cm != "—" else ""
    lines = [("tree-strong", 16, _clip(member.name, 32), member.name)]
    label = member.name
    if detail:
        lines.append(("tree-meta", 14, detail, detail))
        label = f"{member.name}, {detail}"
    return _node("person", x, y, COL_W, lines, label)


def _drop(paths: list[dict], parent_cx: float, parent_bottom: float, targets: list[tuple], stem: str) -> None:
    if not targets:
        return
    bus_y = parent_bottom + 14
    centers = [target[0] for target in targets]
    left = min([parent_cx, *centers])
    right = max([parent_cx, *centers])
    paths.append({"kind": stem, "d": f"M {parent_cx:g} {parent_bottom:g} V {bus_y:g}"})
    if right != left:
        paths.append({"kind": "bus", "d": f"M {left:g} {bus_y:g} H {right:g}"})
    for cx, top, kind in targets:
        paths.append({"kind": kind, "d": f"M {cx:g} {bus_y:g} V {top:g}"})


def _label_from_lines(lines: list[tuple]) -> str:
    return ", ".join(line[3] for line in lines[:2])


def _node(kind: str, x: float, y: float, w: float, lines: list[tuple], label: str) -> dict:
    rendered = []
    for css, line_h, text, full in lines:
        rendered.append({"class": css, "dy": line_h, "text": text, "full": full})
    return {
        "kind": kind,
        "x": x,
        "y": y,
        "w": w,
        "h": _height([(css, line_h) for css, line_h, _text, _full in lines]),
        "label": label,
        "href": "",
        "sex": "",
        "lines": rendered,
    }


def _height(lines: list[tuple]) -> float:
    return 6 + sum(line[1] for line in lines) + 6


def _people_count(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        word = "человек"
    elif count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        word = "человека"
    else:
        word = "человек"
    return f"{count} {word}"


def _clip(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"
