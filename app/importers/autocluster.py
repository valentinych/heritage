from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.importers.tables import decode_table, parse_float, read_delimited, read_xlsx

_JSON = re.compile(r"var\s+json\s*=\s*(\{.*\})\s*;?\s*(?:\nvar\s+tableData|\Z)", re.S)
_MATRIX = re.compile(r"^(\d+)_")
_UUID = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+")


@dataclass
class AutoPerson:
    cluster: str
    name: str
    shared_cm: float | None = None
    kit: str = ""
    segments: int | None = None
    largest_cm: float | None = None
    icw: int | None = None
    relationship: str = ""
    tree_url: str = ""
    tree_count: int | None = None
    note: str = ""


@dataclass
class AutoCluster:
    people: list[AutoPerson] = field(default_factory=list)
    pairs: list[tuple[int, int]] = field(default_factory=list)


def parse_autocluster(filename: str, content: bytes) -> AutoCluster | None:
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix in {".html", ".htm"}:
        return parse_html(content)
    if suffix == ".xlsx":
        from pathlib import Path
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".xlsx") as handle:
            handle.write(content)
            handle.flush()
            headers, records = read_xlsx(Path(handle.name))
        return parse_matrix(headers, records)
    if suffix not in {".csv", ".tsv", ".txt"}:
        return None
    headers, records = read_delimited(decode_table(content))
    return parse_matrix(headers, records)


def parse_html(content: bytes) -> AutoCluster:
    text = content.decode("utf-8", errors="replace")
    match = _JSON.search(text)
    if not match:
        raise ValueError("В файле нет матрицы AutoClusters.")
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError("В файле нет матрицы AutoClusters.") from exc
    nodes = data.get("nodes")
    links = data.get("links")
    if not isinstance(nodes, list) or not nodes or not isinstance(links, list):
        raise ValueError("В файле нет матрицы AutoClusters.")
    people: list[AutoPerson] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        name = _text(node.get("name"), 500)
        group = node.get("group")
        if not name or not isinstance(group, int):
            continue
        people.append(
            AutoPerson(
                cluster=str(group),
                name=name,
                shared_cm=_number(node.get("cM")),
                kit=_kit(node.get("url")),
                segments=_whole(node.get("segments")),
                largest_cm=_number(node.get("largestCm")),
                icw=_whole(node.get("icw")),
                relationship=_text(node.get("groupName"), 120),
                tree_url=_url(node.get("treeId")),
                tree_count=_whole(node.get("personCount")),
                note=_text(node.get("note"), 500),
            )
        )
    if not people:
        raise ValueError("В файле нет матрицы AutoClusters.")
    pairs: set[tuple[int, int]] = set()
    limit = len(people)
    for link in links:
        if not isinstance(link, dict) or not link.get("value"):
            continue
        source = link.get("source")
        target = link.get("target")
        if not isinstance(source, int) or not isinstance(target, int):
            continue
        if source == target or not (0 <= source < limit and 0 <= target < limit):
            continue
        pairs.add((min(source, target), max(source, target)))
    return AutoCluster(people=people, pairs=sorted(pairs))


def parse_matrix(headers: list[str], records: list[dict[str, str]]) -> AutoCluster | None:
    columns = {header.casefold(): header for header in headers}
    cluster_col = _header(columns, ("cluster", "кластер"))
    name_col = _header(columns, ("name", "имя", "фио"))
    matrix = []
    for header in headers:
        match = _MATRIX.match(header)
        if match:
            matrix.append((int(match.group(1)), header))
    if not cluster_col or not name_col or len(matrix) < 3:
        return None
    shared_col = _header(columns, ("total shared cm", "shared cm", "общий cm", "общий см"))
    kit_col = _header(columns, ("identifier", "kit"))
    tree_col = _exact(columns, ("tree", "дерево"))
    count_col = _header(columns, ("tree person count", "людей в дереве"))
    note_col = _header(columns, ("notes", "note", "заметк"))
    segment_col = _header(columns, ("segments", "сегмент"))
    largest_col = _header(columns, ("largest", "крупн"))
    icw_col = _exact(columns, ("icw",))
    relation_col = _header(columns, ("groupname", "relationship", "родство"))
    people: list[AutoPerson] = []
    by_number: dict[int, int] = {}
    for record in records:
        label = (record.get(cluster_col) or "").strip()
        name = _text(record.get(name_col), 500)
        if not label.isdigit() or not name:
            continue
        index = len(people)
        people.append(
            AutoPerson(
                cluster=label,
                name=name,
                shared_cm=parse_float(record.get(shared_col, "")) if shared_col else None,
                kit=_text(record.get(kit_col), 64) if kit_col else "",
                segments=_whole(record.get(segment_col)) if segment_col else None,
                largest_cm=parse_float(record.get(largest_col, "")) if largest_col else None,
                icw=_whole(record.get(icw_col)) if icw_col else None,
                relationship=_text(record.get(relation_col), 120) if relation_col else "",
                tree_url=_url(record.get(tree_col)) if tree_col else "",
                tree_count=_whole(record.get(count_col)) if count_col else None,
                note=_text(record.get(note_col), 500) if note_col else "",
            )
        )
        by_number[index + 1] = index
    if len(people) < 3:
        return None
    pairs: set[tuple[int, int]] = set()
    kept = []
    for record in records:
        label = (record.get(cluster_col) or "").strip()
        name = _text(record.get(name_col), 500)
        if label.isdigit() and name:
            kept.append(record)
    for row_index, record in enumerate(kept):
        for number, header in matrix:
            other = by_number.get(number)
            if other is None or other == row_index:
                continue
            if (record.get(header) or "").strip():
                pairs.add((min(row_index, other), max(row_index, other)))
    return AutoCluster(people=people, pairs=sorted(pairs))


def _header(columns: dict[str, str], keys: tuple[str, ...]) -> str:
    for folded, original in columns.items():
        if _MATRIX.match(original):
            continue
        for key in keys:
            if key in folded:
                return original
    return ""


def _exact(columns: dict[str, str], keys: tuple[str, ...]) -> str:
    for key in keys:
        if key in columns:
            return columns[key]
    return ""


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    clean = " ".join(value.split())
    if not clean or _EMAIL.search(clean):
        return ""
    return clean[:limit]


def _url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    clean = value.split("?", 1)[0].strip()
    if not clean.startswith("http") or _EMAIL.search(clean):
        return ""
    return clean[:1000]


def _kit(value: object) -> str:
    if not isinstance(value, str):
        return ""
    path = value.split("?", 1)[0]
    found = _UUID.findall(path)
    if not found:
        return ""
    return found[-1].upper()[:64]


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return parse_float(value)
    return None


def _whole(value: object) -> int | None:
    number = _number(value)
    if number is None:
        return None
    return int(number)
