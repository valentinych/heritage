from __future__ import annotations

from fastapi import UploadFile

from app.services.storage import clean_filename

MAX_BYTES = 80 * 1024 * 1024
GEDCOM_SUFFIXES = {".ged", ".gedcom"}
TABLE_SUFFIXES = {".csv", ".tsv", ".txt", ".xlsx"}


async def read_upload(file: UploadFile, suffixes: set[str], bad_type: str) -> tuple[str, bytes]:
    if not file.filename:
        raise ValueError("Выберите файл.")
    filename = clean_filename(file.filename)
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix not in suffixes:
        raise ValueError(bad_type)
    content = await file.read()
    if not content:
        raise ValueError("Файл пустой.")
    if len(content) > MAX_BYTES:
        raise ValueError("Файл слишком большой. Максимум 80 МБ.")
    return filename, content
