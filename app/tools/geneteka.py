from __future__ import annotations

import argparse
import getpass
import sys

import httpx

from app.importers.geneteka import fetch_parish


def upload_rows(base_url: str, username: str, password: str, space_id: int, payload: dict, client) -> str:
    base = base_url.rstrip("/")
    login = client.post(f"{base}/login", data={"username": username, "password": password})
    if login.status_code != 303:
        raise RuntimeError("Не удалось войти.")
    response = client.post(f"{base}/s/{space_id}/links/geneteka", json=payload)
    if response.status_code == 200:
        body = response.json()
        message = body.get("message") if isinstance(body, dict) else ""
        if message:
            return str(message)
    detail = ""
    try:
        body = response.json()
        if isinstance(body, dict) and body.get("detail"):
            detail = str(body["detail"])
    except Exception:
        detail = ""
    raise RuntimeError(detail or "Сервер не принял индекс.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Скачать индекс прихода Geneteka")
    parser.add_argument("url")
    parser.add_argument("--upload", metavar="URL", help="Адрес Наследия, например https://62.238.112.84")
    parser.add_argument("--space", type=int)
    parser.add_argument("--user")
    args = parser.parse_args(argv)
    try:
        loaded = fetch_parish(args.url)
    except Exception as exc:
        print(str(exc) or "Не удалось открыть Geneteka.", file=sys.stderr)
        return 1
    print(loaded.message())
    if not args.upload:
        return 0
    if not args.space or not args.user:
        print("Для отправки укажите --space и --user.", file=sys.stderr)
        return 1
    if not loaded.rows:
        return 1
    try:
        password = getpass.getpass("Пароль: ")
    except EOFError:
        print("Введите пароль в терминале.", file=sys.stderr)
        return 1
    if not password:
        print("Пустой пароль.", file=sys.stderr)
        return 1
    try:
        with httpx.Client(timeout=120, follow_redirects=False, verify=False) as client:
            message = upload_rows(args.upload, args.user, password, args.space, loaded.as_payload(), client)
    except Exception as exc:
        print(str(exc) or "Сервер не принял индекс.", file=sys.stderr)
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
