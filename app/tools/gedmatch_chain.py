"""Walk GedMatch one-to-many pages already open in Chrome and store them locally."""

from __future__ import annotations

import fcntl
import random
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

from app.config import Settings
from app.db import create_tables, make_engine, make_session_factory
from app.importers.gedmatch import GedMatchError, parse_kit_owner, parse_one_to_many, rank_kit
from app.services.gedmatch_predict import assign_matrix_kit, replace_hits

START = "BX7602122"
SPACE_ID = 1
CDP = "http://127.0.0.1:9333"
ROOT = Path(__file__).resolve().parents[2]
PAGES = ROOT / "data" / "gedmatch-pages"
FETCH_LOCK = ROOT / "data" / "gedmatch-fetch.lock"
LIMIT_UNTIL = ROOT / "data" / "gedmatch-limit-until"
PRODUCTION = "https://62.238.112.84"


def page_path(kit: str) -> Path:
    return PAGES / f"{kit.upper()}.html"


def read_html(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def saved_kits() -> set[str]:
    if not PAGES.is_dir():
        return set()
    return {path.stem.upper() for path in PAGES.glob("*.html")}


def ensure_legacy_copy() -> None:
    PAGES.mkdir(parents=True, exist_ok=True)
    legacy = ROOT / "data" / "gedmatch-page.html"
    dest = page_path(START)
    if dest.exists() or not legacy.is_file():
        return
    html = read_html(legacy)
    owner = parse_kit_owner(html)
    if owner and owner["kit"] == START:
        dest.write_text(html, encoding="utf-8")


def store_html(db, kit: str, html: str) -> int:
    rows = parse_one_to_many(html)
    owner = parse_kit_owner(html)
    source = (owner or {}).get("kit") or kit
    return replace_hits(db, SPACE_ID, source, rows, owner)


def backfill(db) -> list[str]:
    ensure_legacy_copy()
    done = []
    for path in sorted(PAGES.glob("*.html")):
        kit = path.stem.upper()
        store_html(db, kit, read_html(path))
        done.append(kit)
    return done


def dated_count(db, kit: str) -> int:
    from sqlalchemy import func, select

    from app.models import GedMatchHit

    return int(
        db.scalar(
            select(func.count())
            .select_from(GedMatchHit)
            .where(
                GedMatchHit.space_id == SPACE_ID,
                GedMatchHit.source_kit == kit,
                GedMatchHit.date_compared != "",
            )
        )
        or 0
    )


def match_kits(html: str, own: str) -> list[str]:
    return [row["kit"] for row in parse_one_to_many(html)]


def looks_like_limit(html: str) -> bool:
    low = html.casefold()
    return (
        "you have reached our tool usage limit" in low
        and "please wait one minute and try again" in low
    )


def _limit_deadline() -> float:
    try:
        return float(LIMIT_UNTIL.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0.0


def _stamp_limit() -> None:
    until = time.time() + random.uniform(90, 150)
    LIMIT_UNTIL.parent.mkdir(parents=True, exist_ok=True)
    LIMIT_UNTIL.write_text(f"{until}\n", encoding="utf-8")


def _wait_limit() -> None:
    delay = _limit_deadline() - time.time()
    if delay > 0:
        time.sleep(delay)


@contextmanager
def _fetch_lock():
    FETCH_LOCK.parent.mkdir(parents=True, exist_ok=True)
    handle = FETCH_LOCK.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


_MISSING = (
    "not found",
    "does not exist",
    "no such kit",
    "invalid kit",
    "not in our database",
    "not in the database",
    "was not found",
    "could not find",
    "couldn't find",
    "cannot find",
    "unknown kit",
    "unrecognized kit",
)


def looks_like_missing(html: str) -> bool:
    low = html.casefold()
    if "date compared" in low or ("total cm" in low and "largest seg" in low):
        return False
    return any(marker in low for marker in _MISSING)


def looks_like_login(html: str) -> bool:
    if looks_like_limit(html) or looks_like_missing(html):
        return False
    low = html.casefold()
    has_table = "date compared" in low or ("total cm" in low and "largest seg" in low)
    if has_table:
        return False
    if "auto_logout" in low or "accounts.google.com" in low:
        return True
    return "type=\"password\"" in low or "name=\"password\"" in low


def _closed(exc: BaseException) -> bool:
    return "TargetClosed" in type(exc).__name__ or "has been closed" in str(exc)


def _on_dialog(dialog) -> None:
    # beforeunload must be accepted; dismiss races the driver and wedges the crawl.
    try:
        if dialog.type == "beforeunload":
            dialog.accept()
        else:
            dialog.dismiss()
    except Exception:
        pass


def _following(kits: list[str], kit: str) -> str | None:
    try:
        index = kits.index(kit)
    except ValueError:
        return None
    if index + 1 >= len(kits):
        return None
    return kits[index + 1]


_QUIET = "window.alert=function(){};window.confirm=function(){return true};window.prompt=function(){return null};"


class BrowserSession:
    def __init__(self, playwright) -> None:
        self.playwright = playwright
        self.page = None
        self._context = None
        self.open()

    def open(self) -> None:
        browser = self.playwright.chromium.connect_over_cdp(CDP)
        context = browser.contexts[0]
        if self._context is not context:
            context.on("dialog", self._other_tab)
            self._context = context
        self.page = context.new_page()
        self.page.add_init_script(_QUIET)
        self.page.on("dialog", _on_dialog)

    def _other_tab(self, dialog) -> None:
        if dialog.page == self.page:
            return

    def reopen(self) -> None:
        try:
            if self.page is not None:
                self.page.close()
        except Exception:
            pass
        self.open()

    def close(self) -> None:
        try:
            if self.page is not None:
                self.page.close()
        except Exception:
            pass


def _goto(page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=90000)


def _content(page) -> str:
    last: Exception | None = None
    for _ in range(6):
        try:
            return page.content()
        except Exception as exc:
            if "navigating" not in str(exc).casefold():
                raise
            last = exc
            page.wait_for_timeout(1000)
    raise GedMatchError("timeout content") from last


def _read_kit(page, kit: str) -> str:
    last = ""
    for _ in range(4):
        last = _content(page)
        if looks_like_limit(last):
            return last
        if looks_like_missing(last):
            raise GedMatchError(f"not found {kit}")
        low = last.casefold()
        if "date compared" in low or ("total cm" in low and "largest seg" in low):
            try:
                parse_one_to_many(last)
            except GedMatchError:
                page.wait_for_timeout(5000)
                continue
            return last
        page.wait_for_timeout(5000)
    if looks_like_login(last):
        raise GedMatchError("GedMatch показал страницу входа.")
    raise GedMatchError(f"Таблица не загрузилась: {kit}")


def _load(page, kit: str) -> str:
    url = f"https://app.gedmatch.com/OneToManyFreeOriginalResults.php?kit={kit}"
    limit_tries = 0
    nav_timeouts = 0
    while True:
        _wait_limit()
        with _fetch_lock():
            if _limit_deadline() > time.time():
                action = "wait"
            else:
                try:
                    _goto(page, url)
                except Exception as exc:
                    if type(exc).__name__ != "TimeoutError":
                        raise
                    nav_timeouts += 1
                    if nav_timeouts >= 2:
                        raise GedMatchError(f"timeout {kit}") from exc
                    action = "timeout"
                else:
                    nav_timeouts = 0
                    html = _read_kit(page, kit)
                    if not looks_like_limit(html):
                        return html
                    _stamp_limit()
                    action = "limit"
        if action == "timeout":
            time.sleep(random.uniform(30, 80))
            continue
        if action == "wait":
            continue
        limit_tries += 1
        if limit_tries > 6:
            raise GedMatchError(f"limit {kit}")


def fetch_page(session: BrowserSession, kit: str) -> str:
    for attempt in range(2):
        try:
            return _load(session.page, kit)
        except GedMatchError:
            raise
        except Exception as exc:
            if _closed(exc) and not attempt:
                session.reopen()
                continue
            raise GedMatchError(f"timeout {kit}") from exc
    raise GedMatchError(f"timeout closed {kit}")


def assign_saved(db, name: str, kit: str) -> None:
    from sqlalchemy.exc import OperationalError

    for attempt in range(8):
        try:
            assign_matrix_kit(db, SPACE_ID, name, kit)
            return
        except OperationalError:
            db.rollback()
            if attempt == 7:
                raise
            time.sleep(0.5)


def walk(
    db,
    session: BrowserSession,
    rank: int,
    saved: set[str],
    upload,
    start: str = START,
    matrix: str = "",
) -> dict:
    order: list[str] = []
    newly: list[str] = []
    skipped: list[str] = []
    seen: set[str] = set()
    current = start.strip().upper()
    stopped = ""
    prefix = True
    origin: list[str] = []
    while current and current not in seen:
        seen.add(current)
        path = page_path(current)
        if matrix:
            assign_saved(db, matrix, current)
        if current not in saved and path.is_file():
            saved.add(current)
        if current not in saved:
            time.sleep(random.uniform(30, 80))
            try:
                html = fetch_page(session, current)
            except GedMatchError as exc:
                if "страницу входа" in str(exc):
                    raise
                skipped.append(current)
                print(f"skipped {current} rank {rank}", flush=True)
                nxt = _following(origin, current)
                if nxt is None or nxt in seen or (nxt in saved and not prefix):
                    stopped = nxt or ""
                    break
                current = nxt
                continue
            path.write_text(html, encoding="utf-8")
            store_html(db, current, html)
            saved.add(current)
            newly.append(current)
            prefix = False
            print(f"saved {current} rank {rank}", flush=True)
            upload.one(current)
            if matrix:
                upload.join(current)
        elif matrix:
            upload.join(current)
        order.append(current)
        origin = [kit for kit in match_kits(read_html(path), current) if kit != current.upper()]
        nxt = rank_kit(origin, current, rank)
        if nxt is None or nxt in seen or (nxt in saved and not prefix):
            if matrix and nxt and nxt not in seen and nxt in saved:
                assign_saved(db, matrix, nxt)
                upload.join(nxt)
            stopped = nxt or ""
            break
        current = nxt
    return {"order": order, "new": newly, "stopped": stopped, "skipped": skipped}


def production_client():
    settings = Settings.from_env()
    if not settings.admin_user or not settings.admin_password:
        return None, "production skipped"
    client = httpx.Client(timeout=180, follow_redirects=False, verify=False)
    try:
        login = client.post(
            f"{PRODUCTION}/login",
            data={"username": settings.admin_user, "password": settings.admin_password},
        )
    except Exception:
        client.close()
        return None, "production not updated"
    if login.status_code != 303:
        client.close()
        return None, "production login failed"
    return client, "production ready"


def upload_kit(client, kit: str) -> int | None:
    html = read_html(page_path(kit))
    owner = parse_kit_owner(html)
    rows = parse_one_to_many(html)
    response = client.post(
        f"{PRODUCTION}/s/{SPACE_ID}/gedmatch/import",
        json={"source_kit": kit, "rows": rows, "owner": owner},
    )
    if response.status_code != 200:
        print(f"upload_failed {kit}", flush=True)
        return None
    try:
        stored = int(response.json().get("stored"))
    except Exception:
        print(f"upload_failed {kit}", flush=True)
        return None
    print(f"uploaded {kit} stored {stored}", flush=True)
    return stored


class Uploader:
    def __init__(self, matrix: str = "") -> None:
        self.client = None
        self.failed: list[str] = []
        self.status = "production skipped"
        self.matrix = " ".join(matrix.split())

    def open(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
        self.client, self.status = production_client()

    def one(self, kit: str) -> None:
        if self.client is None:
            self.failed.append(kit)
            print(f"upload_failed {kit}", flush=True)
            return
        try:
            stored = upload_kit(self.client, kit)
        except Exception:
            self.failed.append(kit)
            print(f"upload_failed {kit}", flush=True)
            return
        if stored is None:
            self.failed.append(kit)

    def ensure_matrix(self) -> None:
        if not self.matrix or self.client is None:
            return
        try:
            response = self.client.post(
                f"{PRODUCTION}/s/{SPACE_ID}/gedmatch/matrices",
                data={"name": self.matrix},
            )
        except Exception:
            print("upload_failed matrix", flush=True)
            return
        if response.status_code not in (200, 303):
            print("upload_failed matrix", flush=True)

    def join(self, kit: str) -> None:
        if not self.matrix or self.client is None:
            return
        try:
            response = self.client.post(
                f"{PRODUCTION}/s/{SPACE_ID}/gedmatch/matrices/join",
                json={"name": self.matrix, "kit": kit},
            )
        except Exception:
            print(f"upload_failed {kit}", flush=True)
            return
        if response.status_code != 200:
            print(f"upload_failed {kit}", flush=True)

    def present(self) -> set[str] | None:
        if self.client is None:
            return None
        try:
            response = self.client.get(f"{PRODUCTION}/s/{SPACE_ID}/gedmatch")
        except Exception:
            return None
        if response.status_code != 200:
            return None
        return set(re.findall(r'<option value="([A-Za-z0-9]+)"', response.text))

    def retry(self) -> None:
        pending = list(dict.fromkeys(self.failed))
        if not pending:
            return
        self.failed = []
        self.open()
        for kit in pending:
            self.one(kit)


def _store_saved(db, kit: str, html: str) -> None:
    from sqlalchemy.exc import OperationalError

    for attempt in range(8):
        try:
            store_html(db, kit, html)
            return
        except OperationalError:
            db.rollback()
            if attempt == 7:
                raise
            time.sleep(0.5)


def crawl_once_each(db, session: BrowserSession, kits: list[str], upload: Uploader) -> list[str]:
    """Open one one-to-many page per kit. Does not walk match ranks or join a matrix."""
    saved = saved_kits()
    pending = [kit for kit in kits if kit not in saved]
    newly: list[str] = []
    for index, kit in enumerate(pending):
        if page_path(kit).is_file():
            saved.add(kit)
            continue
        try:
            html = fetch_page(session, kit)
        except GedMatchError as exc:
            if "страницу входа" in str(exc):
                print("login_wall", flush=True)
                return newly
            print(f"skipped {kit}", flush=True)
            continue
        page_path(kit).write_text(html, encoding="utf-8")
        _store_saved(db, kit, html)
        saved.add(kit)
        newly.append(kit)
        print(f"saved {kit}", flush=True)
        upload.one(kit)
        if index + 1 < len(pending):
            time.sleep(random.uniform(30, 80))
    return newly


def novogrudok_main() -> int:
    from app.services.novogrudok import load_column_kits

    kits = [kit.strip().upper() for kit in load_column_kits()]
    saved = saved_kits()
    pending = [kit for kit in kits if kit not in saved]
    print(
        f"novogrudok total {len(kits)} already {len(kits) - len(pending)} open {len(pending)}",
        flush=True,
    )
    if not pending:
        return 0
    settings = Settings.from_env()
    engine = make_engine(settings.data_dir / "heritage.db")
    create_tables(engine)
    db = make_session_factory(engine)()
    upload = Uploader()
    upload.open()
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    session = BrowserSession(pw)
    try:
        crawl_once_each(db, session, kits, upload)
        upload.retry()
    finally:
        session.close()
        pw.stop()
        if upload.client is not None:
            upload.client.close()
        db.close()
    print(f"saved_files {len(saved_kits())}", flush=True)
    print(upload.status if not upload.failed else "production not updated", flush=True)
    return 0


def upload_all(kits: list[str]) -> str:
    upload = Uploader()
    upload.open()
    if upload.client is None:
        return upload.status
    try:
        for kit in kits:
            upload.one(kit)
        upload.retry()
    finally:
        if upload.client is not None:
            upload.client.close()
    if upload.failed:
        return "production not updated"
    return "production updated"


def option(argv: list[str], flag: str, default: str = "") -> str:
    if flag not in argv:
        return default
    index = argv.index(flag)
    if index + 1 >= len(argv):
        return default
    return argv[index + 1]


def rank_span(argv: list[str]) -> range:
    if "--ranks" in argv:
        spec = argv[argv.index("--ranks") + 1]
        start_s, end_s = spec.split("-", 1)
        return range(int(start_s), int(end_s) + 1)
    return range(1, 3)


def main() -> int:
    if "--novogrudok" in sys.argv:
        return novogrudok_main()
    settings = Settings.from_env()
    engine = make_engine(settings.data_dir / "heritage.db")
    create_tables(engine)
    factory = make_session_factory(engine)
    db = factory()
    ranks = rank_span(sys.argv)
    start = option(sys.argv, "--start", START).strip().upper()
    matrix = option(sys.argv, "--matrix", "")
    reports: list[dict] = []
    upload = Uploader(matrix)
    if "--upload" in sys.argv:
        upload.open()
        upload.ensure_matrix()
    try:
        if matrix:
            assign_saved(db, matrix, "")
        else:
            filled = backfill(db)
            for kit in filled:
                print(f"backfill {kit} dated {dated_count(db, kit)}", flush=True)
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        session = BrowserSession(pw)
        try:
            saved = saved_kits()
            for rank in ranks:
                report = walk(db, session, rank, saved, upload, start, matrix)
                report["rank"] = rank
                reports.append(report)
            if "--upload" in sys.argv:
                upload.retry()
        finally:
            session.close()
            pw.stop()
        for report in reports:
            rank = report["rank"]
            print(f"rank{rank} " + ",".join(report["order"]), flush=True)
            print(f"rank{rank}_new " + ",".join(report["new"]), flush=True)
            print(f"rank{rank}_skipped " + ",".join(report["skipped"]), flush=True)
            print(f"rank{rank}_stopped {report['stopped']}", flush=True)
        print(f"saved_files {len(saved_kits())}", flush=True)
        if "--upload" in sys.argv:
            print(upload.status if not upload.failed else "production not updated", flush=True)
    finally:
        if upload.client is not None:
            upload.client.close()
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
