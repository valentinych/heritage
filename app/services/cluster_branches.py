from __future__ import annotations

import re
from itertools import combinations

from app.importers.tables import CLUSTER_FIELDS, guess_mapping

_MATRIX = re.compile(r"^(\d+)_")
_TOKEN = re.compile(r"[^\W\d_]{4,}", re.UNICODE)


def analyze_autocluster(headers: list[str], records: list[dict[str, str]]) -> dict | None:
    guessed = guess_mapping(CLUSTER_FIELDS, headers)
    cluster_col = guessed.get("cluster") or ""
    name_col = guessed.get("name") or ""
    matrix = []
    for header in headers:
        match = _MATRIX.match(header)
        if match:
            matrix.append((int(match.group(1)), header))
    if not cluster_col or not name_col or len(matrix) < 3:
        return None
    rows = []
    for record in records:
        label = (record.get(cluster_col) or "").strip()
        name = (record.get(name_col) or "").strip()
        if not label.isdigit() or not name:
            continue
        rows.append({"label": label, "name": name, "record": record})
    if len(rows) < 3:
        return None
    by_index = {str(index): row for index, row in enumerate(rows, start=1)}
    intra: dict[str, set[frozenset[str]]] = {}
    cross: set[tuple[str, str]] = set()
    names: dict[str, list[str]] = {}
    for row in rows:
        names.setdefault(row["label"], []).append(row["name"])
        for _number, header in matrix:
            other_key = (row["record"].get(header) or "").strip()
            if not other_key or other_key not in by_index:
                continue
            other = by_index[other_key]
            if other["name"] == row["name"]:
                continue
            if other["label"] == row["label"]:
                intra.setdefault(row["label"], set()).add(frozenset((row["name"], other["name"])))
            else:
                cross.add(tuple(sorted((row["label"], other["label"]), key=int)))
    stats = {}
    for label, people in names.items():
        pairs = intra.get(label, set())
        counts = {name: 0 for name in people}
        for pair in pairs:
            for name in pair:
                counts[name] = counts.get(name, 0) + 1
        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        holder = None
        if ranked and ranked[0][1] > 0 and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]):
            holder = ranked[0][0]
        count = len(people)
        stats[label] = {
            "count": len(pairs),
            "possible": count * (count - 1) // 2,
            "holder": holder,
            "pairs": pairs,
            "names": people,
        }
    return {"stats": stats, "cross": cross}


def build_branches(groups: list[dict], analysis: dict | None) -> list[dict] | None:
    if not analysis:
        return None
    by_label = {group["label"]: group for group in groups}
    numeric = [group["label"] for group in groups if str(group["label"]).isdigit() and group["label"] in analysis["stats"]]
    components = _triangle_components(numeric, analysis["cross"])
    used: set[str] = set()
    branches: list[dict] = []
    for component in sorted((item for item in components if len(item) >= 2), key=lambda item: int(item[0])):
        used.update(component)
        branches.append(
            _cluster_branch(component, by_label, analysis, "общий предок ветви {letter}", "для треугольника кластеров")
        )
    for label in numeric:
        if label in used:
            continue
        stat = analysis["stats"][label]
        if stat["possible"] >= 3 and stat["count"] == stat["possible"]:
            used.add(label)
            branches.append(
                _cluster_branch([label], by_label, analysis, "общий предок ветви {letter}", "для внутреннего треугольника людей")
            )
    for label in numeric:
        if label in used:
            continue
        stat = analysis["stats"][label]
        pair = _surname_pair(stat["pairs"])
        members = by_label[label]["members"]
        if pair and len(members) > len(pair):
            used.add(label)
            branches.append(_surname_branch(label, members, pair))
    for index, branch in enumerate(branches):
        letter = chr(ord("A") + index) if index < 26 else str(index + 1)
        ancestor = branch.pop("ancestor").format(letter=letter)
        note = branch.pop("note")
        branch["lines"] = _ancestor_lines(f"Ветвь {letter}", ancestor, "средняя", note, accent=True)
    twigs = [_twig(group, analysis) for group in groups if group["label"] not in used]
    return branches + twigs


def _cluster_branch(labels: list[str], by_label: dict, analysis: dict, ancestor: str, note: str) -> dict:
    columns = []
    for label in labels:
        group = by_label[label]
        columns.append(
            {
                "lines": _cluster_lines(label, group["members"], analysis["stats"].get(label)),
                "members": group["members"],
                "link": "link",
            }
        )
    return {"kind": "branch", "link": "link", "ancestor": ancestor, "note": note, "columns": columns}


def _surname_branch(label: str, members: list, pair: frozenset[str]) -> dict:
    names = set(pair)
    left = [member for member in members if member.name in names]
    right = [member for member in members if member.name not in names]
    return {
        "kind": "branch",
        "link": "link",
        "ancestor": "общий предок пары ветви {letter}",
        "note": "только для пары с общей основой фамилии",
        "columns": [
            {
                "lines": [
                    ("tree-cluster", 15, "под предком пары", "под предком пары"),
                    ("tree-accent", 13, "уверенность: средняя", "уверенность: средняя"),
                    ("tree-note", 13, f"кластер {label}", f"кластер {label}"),
                ],
                "members": left,
                "link": "link",
            },
            {
                "lines": [
                    ("tree-cluster", 15, "не под предком пары", "не под предком пары"),
                    ("tree-note", 13, "уверенность: низкая", "уверенность: низкая"),
                    ("tree-note", 13, "общая основа фамилии в имени не видна", "общая основа фамилии в имени не видна"),
                ],
                "members": right,
                "link": "link-low",
            },
        ],
    }


def _twig(group: dict, analysis: dict) -> dict:
    label = group["label"]
    stat = analysis["stats"].get(label)
    note = f"кластер {label}, отдельно от ветвей" if str(label).isdigit() else str(label)
    return {
        "kind": "twig",
        "link": "link-low",
        "lines": _ancestor_lines("отдельная веточка", "общий предок веточки", "низкая", note, accent=False),
        "columns": [
            {
                "lines": _cluster_lines(label, group["members"], stat),
                "members": group["members"],
                "link": "link-low",
            }
        ],
    }


def _ancestor_lines(title: str, ancestor: str, confidence: str, note: str, accent: bool) -> list[tuple]:
    title_class = "tree-accent" if accent else "tree-meta"
    return [
        (title_class, 14, title, title),
        ("tree-strong", 16, ancestor, ancestor),
        ("tree-note", 13, f"уверенность: {confidence}", f"уверенность: {confidence}"),
        ("tree-note", 13, note, note),
    ]


def _cluster_lines(label: str, members: list, stat: dict | None) -> list[tuple]:
    title = f"Кластер {label}"
    lines = [("tree-cluster", 15, title, title)]
    phrase = _pair_phrase(stat) if stat else _people_count(len(members))
    lines.append(("tree-meta", 13, _clip(phrase, 42), phrase))
    return lines


def _pair_phrase(stat: dict) -> str:
    pairs = stat["count"]
    possible = stat["possible"]
    if possible <= 0:
        return _people_count(len(stat["names"]))
    base = f"{pairs} из {possible}"
    if pairs == possible:
        return f"{base} · все пары совпадают"
    if possible and pairs / possible >= 0.8:
        return f"{base} · почти все пары"
    if stat["holder"]:
        return f"{base} · держится на {stat['holder']}"
    return f"{base} · не все пары"


def _triangle_components(labels: list[str], edges: set[tuple[str, str]]) -> list[list[str]]:
    parent = {label: label for label in labels}
    linked = {label: set() for label in labels}

    def find(label: str) -> str:
        while parent[label] != label:
            parent[label] = parent[parent[label]]
            label = parent[label]
        return label

    def union(left: str, right: str) -> None:
        parent[find(left)] = find(right)

    for left, right in edges:
        if left in linked and right in linked:
            linked[left].add(right)
            linked[right].add(left)
    for left, middle, right in combinations(labels, 3):
        if middle in linked[left] and right in linked[left] and right in linked[middle]:
            union(left, middle)
            union(left, right)
    groups: dict[str, list[str]] = {}
    for label in labels:
        groups.setdefault(find(label), []).append(label)
    return [sorted(group, key=int) for group in groups.values()]


def _surname_pair(pairs: set[frozenset[str]]) -> frozenset[str] | None:
    found = None
    for pair in pairs:
        names = tuple(pair)
        if len(names) != 2 or not _share_stem(names[0], names[1]):
            continue
        if found is not None:
            return None
        found = pair
    return found


def _share_stem(left: str, right: str) -> bool:
    for left_token in _TOKEN.findall(left.casefold()):
        for right_token in _TOKEN.findall(right.casefold()):
            size = 0
            limit = min(len(left_token), len(right_token))
            while size < limit and left_token[size] == right_token[size]:
                size += 1
            if size >= 7:
                return True
    return False


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
