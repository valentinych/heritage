from __future__ import annotations


def person_key(name: str) -> str:
    return " ".join(name.casefold().split())


def split_for_self(items: list[dict], self_name: str) -> tuple[list[dict], list[dict]]:
    """Files that share a person name, directly or through another file, stay with self."""
    key = person_key(self_name)
    parent = list(range(len(items)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        parent[find(left)] = find(right)

    keys = [{person_key(name) for name in item["names"]} for item in items]
    for left in range(len(items)):
        for right in range(left + 1, len(items)):
            if keys[left] & keys[right]:
                union(left, right)
    seeds = [index for index, item in enumerate(items) if person_key(item.get("owner") or "") == key]
    if not seeds:
        return [], list(items)
    root = find(seeds[0])
    connected = [items[index] for index in range(len(items)) if find(index) == root]
    separate = [items[index] for index in range(len(items)) if find(index) != root]
    return connected, separate
