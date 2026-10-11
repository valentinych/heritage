from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

_SUFFIX = re.compile(r"\.[a-z0-9]{1,10}")


def clean_filename(name: str) -> str:
    base = Path(name).name.replace("\x00", "")
    base = " ".join(base.split())
    if not base:
        base = "file"
    return base[:255]


def save_bytes(data_dir: Path, space_id: int, filename: str, content: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if _SUFFIX.fullmatch(suffix) is None:
        suffix = ""
    relative = Path("uploads") / str(space_id) / f"{uuid4().hex}{suffix}"
    target = data_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return relative.as_posix()


def resolve_path(data_dir: Path, relative: str) -> Path:
    root = (data_dir / "uploads").resolve()
    path = (data_dir / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise FileNotFoundError(relative)
    return path


def remove_file(data_dir: Path, relative: str) -> None:
    try:
        resolve_path(data_dir, relative).unlink()
    except FileNotFoundError:
        return
