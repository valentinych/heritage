from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlencode


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def paginate(page: int, per_page: int, total: int) -> tuple[int, int, int]:
    pages = max(1, (total + per_page - 1) // per_page) if total else 1
    current = min(max(page, 1), pages)
    offset = (current - 1) * per_page
    return current, pages, offset


def pager(path: str, page: int, pages: int, **params: str) -> dict[str, object]:
    page_key = params.pop("page_key", "page")

    def href(number: int) -> str:
        query = {page_key: str(number)}
        for key, value in params.items():
            if value:
                query[key] = value
        return f"{path}?{urlencode(query)}"

    return {
        "page": page,
        "pages": pages,
        "prev_href": href(page - 1) if page > 1 else None,
        "next_href": href(page + 1) if page < pages else None,
    }
