from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

import httpx

from app.importers.gedmatch import GedMatchError, parse_kit_owner, parse_one_to_many


def fetch_in_browser(kit: str, profile: Path) -> str:
    from playwright.sync_api import sync_playwright

    url = f"https://app.gedmatch.com/OneToManyFreeOriginalResults.php?kit={kit}"
    pw = sync_playwright().start()
    context = pw.chromium.launch_persistent_context(
        str(profile),
        channel="chrome",
        headless=False,
        viewport=None,
        timeout=60000,
        ignore_default_args=["--enable-automation"],
        args=[
            "--no-restore-last-session",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        print("Если GedMatch просит вход, войдите в открытом окне Chrome.", flush=True)
        for _ in range(120):
            page.wait_for_timeout(5000)
            try:
                html = page.content()
            except Exception:
                continue
            if "total cm" in html.casefold() or "largest seg" in html.casefold():
                return html
        raise GedMatchError("Таблица one-to-many не появилась.")
    finally:
        context.close()
        pw.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Разобрать one-to-many GedMatch")
    parser.add_argument("--kit", default="BX7602122")
    parser.add_argument("--html", type=Path)
    parser.add_argument("--upload", metavar="URL")
    parser.add_argument("--space", type=int)
    parser.add_argument("--user")
    args = parser.parse_args(argv)
    try:
        if args.html:
            html = args.html.read_text(encoding="utf-8", errors="replace")
        else:
            html = fetch_in_browser(args.kit, Path("data/gedmatch-profile").resolve())
        rows = parse_one_to_many(html)
        owner = parse_kit_owner(html)
    except Exception as exc:
        print(str(exc) or "Не удалось разобрать GedMatch.", file=sys.stderr)
        return 1
    print(f"Совпадений: {len(rows)}.")
    if not args.upload:
        return 0
    if not args.space or not args.user:
        print("Для отправки укажите --space и --user.", file=sys.stderr)
        return 1
    try:
        password = getpass.getpass("Пароль: ")
    except EOFError:
        print("Введите пароль в терминале.", file=sys.stderr)
        return 1
    payload = {"source_kit": owner.get("kit") if owner else args.kit, "rows": rows, "owner": owner}
    try:
        with httpx.Client(timeout=120, follow_redirects=False, verify=False) as client:
            base = args.upload.rstrip("/")
            login = client.post(f"{base}/login", data={"username": args.user, "password": password})
            if login.status_code != 303:
                raise RuntimeError("Не удалось войти.")
            response = client.post(f"{base}/s/{args.space}/gedmatch/import", json=payload)
    except Exception as exc:
        print(str(exc) or "Сервер не принял таблицу.", file=sys.stderr)
        return 1
    if response.status_code != 200:
        print("Сервер не принял таблицу.", file=sys.stderr)
        return 1
    print(response.json().get("message", "Сохранено."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
