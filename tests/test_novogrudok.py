import re
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings
from app.deps import KINSHIP_EDITOR_EMAIL
from app.main import create_app
from app.models import GedMatchHit, GedMatchKinship, GedMatchKit, Person, Tree, User
from app.util import utcnow
from app.services.gedmatch_predict import apply_manual, forecast_pair
from tests.test_heritage import find_space_id, find_user_id, login


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        secret="test-secret-value",
        data_dir=tmp_path,
        admin_user="admin",
        admin_password="password123",
        cookie_secure=False,
        production=False,
    )
    application = create_app(settings)
    with TestClient(application) as test_client:
        yield test_client


def test_novogrudok_is_public_without_login(client, monkeypatch):
    monkeypatch.setattr("app.routes.novogrudok.load_column_kits", lambda: ["A1111111", "BX7602122"])
    page = client.get("/novogrudok", follow_redirects=False)
    assert page.status_code == 200
    assert page.headers.get("location") is None
    assert 'href="/novogrudok"' in page.text
    assert 'href="/novogrudok?show=cm"' in page.text
    assert "Gen" in page.text
    assert "cM" in page.text
    assert "Новогрудок" in page.text
    assert "Исследовано 0/2 наборов" in page.text
    assert 'http-equiv="refresh"' not in page.text
    denied = client.get("/s/1/gedmatch/clusters", follow_redirects=False)
    assert denied.status_code == 303
    assert urlparse(denied.headers["location"]).path == "/login"


def test_novogrudok_toggle_shows_gen_or_cm(client, monkeypatch):
    monkeypatch.setattr(
        "app.routes.novogrudok.load_column_kits",
        lambda: ["A1111111", "BX7602122", "C3333333"],
    )
    login(client)
    created = client.post("/admin/spaces", data={"name": "Публичная матрица", "add_self": "on"}, follow_redirects=True)
    space_id = find_space_id(created.text, "Публичная матрица")
    assert space_id == 1
    db = client.app.state.session_factory()
    try:
        db.add(
            GedMatchKit(
                space_id=space_id,
                source_kit="A1111111",
                name="Kit Owner",
                email="hidden@example.com",
            )
        )
        db.add(
            GedMatchHit(
                space_id=space_id,
                source_kit="A1111111",
                match_kit="BX7602122",
                name="Match Label",
                total_cm=180.5,
                generations=3.2,
            )
        )
        db.commit()
    finally:
        db.close()
    client.post("/logout", follow_redirects=False)

    page = client.get("/novogrudok", follow_redirects=False)
    assert page.status_code == 200
    assert urlparse(page.headers.get("location") or "").path != "/login"
    assert "Исследовано 1/3 наборов" in page.text
    assert page.text.count("3.2") == 2
    assert "180.5" not in page.text
    assert page.text.count("gedmatch-gen-4") == 2
    assert "gedmatch-matrix-self" in page.text
    assert "C3333333" in page.text
    assert "hidden@example.com" not in page.text
    assert "Kit Owner" not in page.text
    assert "Match Label" not in page.text
    assert "novogrudok-name" not in page.text
    assert "gedmatch-matrix-name" not in page.text
    for kit in ("A1111111", "BX7602122", "C3333333"):
        assert f'href="{_gedmatch_kit(kit)}"' in page.text
        assert f">{kit}</a>" in page.text
    assert 'href="/s/' not in page.text
    assert "onclick" not in page.text
    pair = "/novogrudok/A1111111/BX7602122"
    reverse = "/novogrudok/BX7602122/A1111111"
    assert f'href="{pair}"' in page.text
    assert f'href="{reverse}"' in page.text
    detail = client.get(pair, follow_redirects=False)
    assert detail.status_code == 200
    assert "180.5" in detail.text
    assert "hidden@example.com" not in detail.text
    assert "Kit Owner" not in detail.text
    assert "Match Label" not in detail.text
    assert f'href="{_gedmatch_kit("A1111111")}"' in detail.text
    assert f'href="{_gedmatch_kit("BX7602122")}"' in detail.text
    assert 'href="/novogrudok"' in detail.text
    assert 'href="/s/' not in detail.text
    assert 'http-equiv="refresh"' not in detail.text
    other = client.get(reverse, follow_redirects=False)
    assert other.status_code == 200
    assert "180.5" in other.text
    assert "3.2" in other.text
    unit = re.search(r'<nav class="unit-switch".*?</nav>', page.text, re.S)
    assert unit
    gen_link = re.search(r'<a[^>]*href="/novogrudok"[^>]*>', unit.group(0))
    cm_link = re.search(r'<a[^>]*href="/novogrudok\?show=cm"[^>]*>', unit.group(0))
    assert gen_link and "aria-current" in gen_link.group(0)
    assert cm_link and "aria-current" not in cm_link.group(0)

    shown = client.get("/novogrudok?show=cm", follow_redirects=False)
    assert shown.status_code == 200
    assert shown.text.count("180.5") == 2
    assert "3.2" not in shown.text
    assert "gedmatch-gen-4" in shown.text
    unit = re.search(r'<nav class="unit-switch".*?</nav>', shown.text, re.S)
    assert unit
    gen_link = re.search(r'<a[^>]*href="/novogrudok"[^>]*>', unit.group(0))
    cm_link = re.search(r'<a[^>]*href="/novogrudok\?show=cm"[^>]*>', unit.group(0))
    assert gen_link and "aria-current" not in gen_link.group(0)
    assert cm_link and "aria-current" in cm_link.group(0)
    assert "Kit Owner" not in shown.text
    assert "Match Label" not in shown.text
    assert f'href="{_gedmatch_kit("A1111111")}"' in shown.text


def _gedmatch_kit(kit: str) -> str:
    return f"https://app.gedmatch.com/OneToManyFreeOriginalResults.php?kit={kit}"


def _visible(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html)


def _matrix_cells(html: str) -> dict[tuple[str, str], str]:
    columns = re.findall(r'<a class="novogrudok-kit" href="[^"]*">([^<]+)</a>', html)
    body = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    cells: dict[tuple[str, str], str] = {}
    for chunk in body.split('<a class="gedmatch-matrix-kit"')[1:]:
        found = re.match(r' href="[^"]*">([^<]+)</a>', chunk)
        assert found
        kit = found.group(1)
        rest = chunk.split("</a>", 1)[1]
        row_html = rest.split("</tr>", 1)[0]
        tds = re.findall(r"<td\b([^>]*)>(.*?)</td>", row_html, re.S)
        assert len(tds) == len(columns)
        for column, (attrs, inner) in zip(columns, tds):
            cells[(kit, column)] = f"{attrs}>{inner}"
    return cells


def test_novogrudok_matrix_mirrors_one_direction(client, monkeypatch):
    kits = ["DD4162841", "QU7039849", "AA1111111", "BB2222222", "CC3333333"]
    monkeypatch.setattr("app.routes.novogrudok.load_column_kits", lambda: kits)
    login(client)
    created = client.post("/admin/spaces", data={"name": "Зеркало", "add_self": "on"}, follow_redirects=True)
    space_id = find_space_id(created.text, "Зеркало")
    assert space_id == 1
    _add_hit(client, space_id, "DD4162841", "QU7039849", 180.5, 3.2)
    _add_hit(client, space_id, "AA1111111", "BB2222222", 111.1, 2.2)
    _add_hit(client, space_id, "BB2222222", "AA1111111", 222.2, 4.5)
    client.post("/logout", follow_redirects=False)

    page = client.get("/novogrudok", follow_redirects=False)
    assert page.status_code == 200
    cells = _matrix_cells(page.text)
    later = cells[("DD4162841", "QU7039849")]
    earlier = cells[("QU7039849", "DD4162841")]
    assert 'href="/novogrudok/DD4162841/QU7039849"' in later
    assert 'href="/novogrudok/QU7039849/DD4162841"' in earlier
    assert ">3.2</a>" in later and ">3.2</a>" in earlier
    assert "gedmatch-gen-4" in later and "gedmatch-gen-4" in earlier
    assert "180.5" not in later and "180.5" not in earlier
    assert ">2.2</a>" in cells[("AA1111111", "BB2222222")]
    assert ">4.5</a>" in cells[("BB2222222", "AA1111111")]
    assert "gedmatch-gen-3" in cells[("AA1111111", "BB2222222")]
    assert "gedmatch-gen-5" in cells[("BB2222222", "AA1111111")]
    for kit in kits:
        assert 'href=' not in cells[(kit, kit)]
        assert "gedmatch-matrix-self" in cells[(kit, kit)]
    for other in kits:
        if other == "CC3333333":
            continue
        assert cells[("CC3333333", other)].endswith(">")
        assert cells[(other, "CC3333333")].endswith(">")

    shown = client.get("/novogrudok?show=cm", follow_redirects=False)
    cm_cells = _matrix_cells(shown.text)
    assert ">180.5</a>" in cm_cells[("DD4162841", "QU7039849")]
    assert ">180.5</a>" in cm_cells[("QU7039849", "DD4162841")]
    assert "gedmatch-gen-4" in cm_cells[("QU7039849", "DD4162841")]
    assert ">111.1</a>" in cm_cells[("AA1111111", "BB2222222")]
    assert ">222.2</a>" in cm_cells[("BB2222222", "AA1111111")]
    assert "3.2" not in shown.text
    detail = client.get("/novogrudok/QU7039849/DD4162841", follow_redirects=False)
    assert detail.status_code == 200
    assert "180.5" in detail.text
    assert "3.2" in detail.text


def _set_email(client, username: str, email: str) -> None:
    db = client.app.state.session_factory()
    account = db.scalar(select(User).where(User.username == username))
    account.email = email
    db.commit()
    db.close()


def _kinship_count(client) -> int:
    db = client.app.state.session_factory()
    try:
        return db.scalar(select(func.count()).select_from(GedMatchKinship))
    finally:
        db.close()


def _add_hit(
    client, space_id: int, source: str, match: str, cm: float, gen: float | None = None, name: str = ""
) -> None:
    db = client.app.state.session_factory()
    db.add(
        GedMatchHit(
            space_id=space_id,
            source_kit=source,
            match_kit=match,
            name=name,
            total_cm=cm,
            generations=gen,
        )
    )
    db.commit()
    db.close()


def _add_owner(client, space_id: int, kit: str, name: str, email: str = "") -> None:
    db = client.app.state.session_factory()
    db.add(GedMatchKit(space_id=space_id, source_kit=kit, name=name, email=email))
    db.commit()
    db.close()


def test_novogrudok_pair_forecast_respects_parent_pairs(client):
    login(client)
    opened = client.post("/admin/spaces", data={"name": "Новогрудок", "add_self": "on"}, follow_redirects=True)
    space_id = find_space_id(opened.text, "Новогрудок")
    assert space_id == 1
    _add_hit(client, space_id, "MOTHER01", "CHLD1111", 3400, 1)
    _add_hit(client, space_id, "FATHER01", "CHLD1111", 3300, 1)
    _add_hit(client, space_id, "MOTHER01", "FATHER01", 2600, 1)
    _add_hit(client, space_id, "DAD00001", "KID00001", 3400, 1)
    _add_hit(client, space_id, "MOM00001", "KID00001", 3300, 1)
    _add_hit(client, space_id, "DAD00001", "MOM00001", 12, 5)
    client.post("/logout", follow_redirects=False)

    adults = client.get("/novogrudok/MOTHER01/FATHER01", follow_redirects=False)
    assert adults.status_code == 200
    assert "двое родителей" not in adults.text
    assert "брат или сестра" in adults.text
    assert "Сохранить связь" not in adults.text

    parents = client.get("/novogrudok/DAD00001/MOM00001", follow_redirects=False)
    assert parents.status_code == 200
    assert "двое родителей набора KID00001" in _visible(parents.text)
    assert "родственная связь не выводится" in parents.text
    assert "брат или сестра" not in parents.text

    child = client.get("/novogrudok/DAD00001/KID00001", follow_redirects=False)
    assert child.status_code == 200
    assert "Расстояние в поколениях: 1." in child.text
    assert "MOM00001" in child.text
    assert "Сохранить связь" not in child.text


def test_novogrudok_kinship_edit_is_limited_to_one_account(client):
    login(client)
    created = client.post(
        "/admin/users",
        data={"username": "anna", "password": "password123"},
        follow_redirects=True,
    )
    find_user_id(created.text, "anna")
    opened = client.post("/admin/spaces", data={"name": "Новогрудок", "add_self": "on"}, follow_redirects=True)
    space_id = find_space_id(opened.text, "Новогрудок")
    assert space_id == 1
    db = client.app.state.session_factory()
    try:
        db.add(
            GedMatchHit(
                space_id=space_id,
                source_kit="MOTHER01",
                match_kit="CHLD1111",
                name="Анна",
                total_cm=3400,
                generations=1,
            )
        )
        db.add(
            GedMatchKit(
                space_id=space_id,
                source_kit="MOTHER01",
                name="Мария",
                email="hidden@example.com",
            )
        )
        db.commit()
    finally:
        db.close()
    client.post("/logout", follow_redirects=False)

    pair = "/novogrudok/MOTHER01/CHLD1111"
    form = {"source_kit": "MOTHER01", "match_kit": "CHLD1111", "relation": "mo_d:L"}
    anon = client.get(pair, follow_redirects=False)
    assert anon.status_code == 200
    assert "Сохранить связь" not in anon.text
    assert 'action="/novogrudok/kinship"' not in anon.text
    assert 'name="relation"' not in anon.text
    assert "родитель или ребёнок" in anon.text
    assert KINSHIP_EDITOR_EMAIL not in anon.text
    assert " кит" not in anon.text
    blocked = client.post("/novogrudok/kinship", data=form, follow_redirects=False)
    assert blocked.status_code == 403
    denied_delete = client.post(
        "/novogrudok/kinship/delete",
        data={"source_kit": "MOTHER01", "match_kit": "CHLD1111"},
        follow_redirects=False,
    )
    assert denied_delete.status_code == 403
    assert _kinship_count(client) == 0

    login(client, "anna", "password123")
    viewer = client.get(pair, follow_redirects=False)
    assert viewer.status_code == 200
    assert "Сохранить связь" not in viewer.text
    assert 'name="relation"' not in viewer.text
    assert KINSHIP_EDITOR_EMAIL not in viewer.text
    again = client.post("/novogrudok/kinship", data=form, follow_redirects=False)
    assert again.status_code == 403
    assert _kinship_count(client) == 0
    client.post("/logout", follow_redirects=False)

    login(client)
    _set_email(client, "admin", KINSHIP_EDITOR_EMAIL)
    editor = client.get(pair, follow_redirects=False)
    assert editor.status_code == 200
    assert "Сохранить связь" in editor.text
    assert 'action="/novogrudok/kinship"' in editor.text
    mother = "MOTHER01"
    child = "CHLD1111"
    named_mother = f"{mother} (Мария)"
    named_child = f"{child} (Анна)"
    assert f"{mother} — мать, {child} — дочь" in _visible(editor.text)
    assert f"{child} — отец, {mother} — сын" in _visible(editor.text)
    assert "Мария" not in editor.text
    assert "Анна" not in editor.text
    assert "()" not in editor.text
    assert KINSHIP_EDITOR_EMAIL not in editor.text
    assert "hidden@example.com" not in editor.text
    assert " кит" not in editor.text
    saved = client.post("/novogrudok/kinship", data=form, follow_redirects=False)
    assert saved.status_code == 303
    assert urlparse(saved.headers["location"]).path == pair
    shown = client.get(pair, follow_redirects=False)
    assert "Связь сохранена." in shown.text
    assert f"Сохранённая связь: {mother} — мать, {child} — дочь. Расстояние в поколениях: 1." in _visible(shown.text)
    assert "Гипотеза по cM:" in shown.text
    assert "Удалить связь" in shown.text
    ged = client.get(f"/s/{space_id}/gedmatch/clusters/MOTHER01/CHLD1111", follow_redirects=False)
    assert ged.status_code == 200
    assert f"Сохранённая связь: {named_mother} — мать, {named_child} — дочь. Расстояние в поколениях: 1." in ged.text
    assert "Гипотеза по cM:" in ged.text
    db = client.app.state.session_factory()
    try:
        rows = list(db.scalars(select(GedMatchKinship).where(GedMatchKinship.space_id == space_id)))
        assert len(rows) == 1
        assert rows[0].relation == "mo_d"
        assert rows[0].parent_kit == "MOTHER01"
        assert rows[0].kit_low == "CHLD1111"
        assert rows[0].kit_high == "MOTHER01"
    finally:
        db.close()

    client.post("/logout", follow_redirects=False)
    public = client.get(pair, follow_redirects=False)
    assert public.status_code == 200
    assert f"Сохранённая связь: {mother} — мать, {child} — дочь. Расстояние в поколениях: 1." in _visible(public.text)
    assert "Мария" not in public.text
    assert "Анна" not in public.text
    assert "hidden@example.com" not in public.text
    assert "Гипотеза по cM:" in public.text
    assert "Сохранить связь" not in public.text
    assert 'action="/novogrudok/kinship"' not in public.text

    login(client, "anna", "password123")
    removed = client.post(
        "/novogrudok/kinship/delete",
        data={"source_kit": "MOTHER01", "match_kit": "CHLD1111"},
        follow_redirects=False,
    )
    assert removed.status_code == 403
    assert _kinship_count(client) == 1
    client.post("/logout", follow_redirects=False)

    login(client)
    gone = client.post(
        "/novogrudok/kinship/delete",
        data={"source_kit": "MOTHER01", "match_kit": "CHLD1111"},
        follow_redirects=False,
    )
    assert gone.status_code == 303
    assert urlparse(gone.headers["location"]).path == pair
    cleared = client.get(pair, follow_redirects=False)
    assert "Связь удалена." in cleared.text
    assert "Сохранённая связь" not in cleared.text
    assert "родитель или ребёнок" in cleared.text
    assert _kinship_count(client) == 0


def test_novogrudok_second_generation_path_is_one_row(client):
    login(client)
    client.post(
        "/admin/users",
        data={"username": "anna", "password": "password123"},
        follow_redirects=True,
    )
    opened = client.post("/admin/spaces", data={"name": "Новогрудок", "add_self": "on"}, follow_redirects=True)
    space_id = find_space_id(opened.text, "Новогрудок")
    assert space_id == 1
    _add_hit(client, space_id, "GMOTHER1", "GCHILD01", 1700, 2.4)
    _add_hit(client, space_id, "GCHILD01", "GMOTHER1", 1700, 2.4)
    client.post("/logout", follow_redirects=False)

    elder = "/novogrudok/GMOTHER1/GCHILD01"
    younger = "/novogrudok/GCHILD01/GMOTHER1"
    row, column = "GMOTHER1", "GCHILD01"
    options = (
        f"{row} — бабушка (мать матери), {column} — внучка",
        f"{column} — бабушка (мать матери), {row} — внучка",
        f"{row} — бабушка (мать матери), {column} — внук",
        f"{column} — дедушка (отец матери), {row} — внучка",
        f"{row} — бабушка (мать отца), {column} — внук",
        f"{column} — дедушка (отец отца), {row} — внучка",
        f"{row} — мать, {column} — дочь",
        f"{column} — отец, {row} — сын",
        f"{row} — брат, {column} — сестра",
        f"{row} — сестра, {column} — брат",
        f"{row} — брат, {column} — брат",
        f"{row} — сестра, {column} — сестра",
        f"{row} и {column} — полусиблинг или та же полоса cM",
        f"{row} и {column} — дальнее родство",
        f"{row} и {column} — не родственники",
    )
    phrase = f"{row} — бабушка (мать матери), {column} — внучка"
    grandmother = {
        "source_kit": row,
        "match_kit": column,
        "relation": "mm_f:L",
    }
    grandchild = {
        "source_kit": column,
        "match_kit": row,
        "relation": "mm_f:R",
    }

    anon = client.get(elder, follow_redirects=False)
    assert anon.status_code == 200
    assert "Сохранить связь" not in anon.text
    assert 'action="/novogrudok/kinship"' not in anon.text
    assert 'name="relation"' not in anon.text
    assert KINSHIP_EDITOR_EMAIL not in anon.text

    login(client, "anna", "password123")
    blocked = client.post("/novogrudok/kinship", data=grandmother, follow_redirects=False)
    assert blocked.status_code == 403
    assert _kinship_count(client) == 0
    client.post("/logout", follow_redirects=False)

    login(client)
    _set_email(client, "admin", KINSHIP_EDITOR_EMAIL)
    editor = client.get(elder, follow_redirects=False)
    assert editor.status_code == 200
    for label in options:
        assert label in editor.text
    assert f"{row} (" not in editor.text
    assert f"{column} (" not in editor.text
    assert "()" not in editor.text
    assert "внук или внучка совпадения" not in editor.text
    assert "Этот набор —" not in editor.text
    assert KINSHIP_EDITOR_EMAIL not in editor.text
    assert " кит" not in editor.text

    saved = client.post("/novogrudok/kinship", data=grandmother, follow_redirects=False)
    assert saved.status_code == 303
    assert urlparse(saved.headers["location"]).path == elder
    shown = client.get(elder, follow_redirects=False)
    assert f"Сохранённая связь: {phrase}. Расстояние в поколениях: 2." in _visible(shown.text)
    assert "Гипотеза по cM:" in shown.text
    hypothesis = shown.text.split("Гипотеза по cM:", 1)[1].split("</p>", 1)[0]
    assert "мать матери" not in hypothesis
    assert "внук или внучка" in hypothesis
    ged = client.get(f"/s/{space_id}/gedmatch/clusters/GMOTHER1/GCHILD01", follow_redirects=False)
    assert ged.status_code == 200
    assert phrase in ged.text

    client.post("/logout", follow_redirects=False)
    public = client.get(elder, follow_redirects=False)
    assert public.status_code == 200
    assert phrase in _visible(public.text)
    assert f"{row} (" not in public.text
    assert f"{column} (" not in public.text
    assert "Расстояние в поколениях: 2." in public.text
    assert "Сохранить связь" not in public.text
    assert 'name="relation"' not in public.text
    assert KINSHIP_EDITOR_EMAIL not in public.text

    login(client, "anna", "password123")
    again = client.post("/novogrudok/kinship", data=grandchild, follow_redirects=False)
    assert again.status_code == 403
    assert _kinship_count(client) == 1
    client.post("/logout", follow_redirects=False)

    login(client)
    inverse = client.post("/novogrudok/kinship", data=grandchild, follow_redirects=False)
    assert inverse.status_code == 303
    assert urlparse(inverse.headers["location"]).path == younger
    db = client.app.state.session_factory()
    try:
        rows = list(db.scalars(select(GedMatchKinship).where(GedMatchKinship.space_id == space_id)))
        assert len(rows) == 1
        assert rows[0].relation == "gp_mm_f"
        assert rows[0].parent_kit == "GMOTHER1"
    finally:
        db.close()
    child_page = client.get(younger, follow_redirects=False)
    assert f"Сохранённая связь: {phrase}. Расстояние в поколениях: 2." in _visible(child_page.text)
    elder_page = client.get(elder, follow_redirects=False)
    assert phrase in elder_page.text
    ged_child = client.get(f"/s/{space_id}/gedmatch/clusters/GCHILD01/GMOTHER1", follow_redirects=False)
    assert phrase in ged_child.text

    forecast = forecast_pair({("GMOTHER1", "GCHILD01"): (1700.0, None)}, "GMOTHER1", "GCHILD01")
    merged = apply_manual(forecast, "gp_mm_f", "GMOTHER1", "GMOTHER1", "GCHILD01")
    assert merged["generation_distance"] == 2
    assert merged["conclusion_relation"] == "gp_mm_f"
    assert "мать матери" not in merged["hypothesis"]["summary"]
    flipped = apply_manual(forecast, "gp_mm_f", "GMOTHER1", "GCHILD01", "GMOTHER1")
    assert flipped["generation_distance"] == 2
    assert flipped["saved_choice"] == "mm_f:R"


def test_pair_form_assigns_grandmother_and_granddaughter(client):
    login(client)
    client.post(
        "/admin/users",
        data={"username": "anna", "password": "password123"},
        follow_redirects=True,
    )
    opened = client.post("/admin/spaces", data={"name": "Новогрудок", "add_self": "on"}, follow_redirects=True)
    space_id = find_space_id(opened.text, "Новогрудок")
    assert space_id == 1
    row, column = "T594513", "T631421"
    _add_hit(client, space_id, row, column, 1700, 2.4)
    _add_hit(client, space_id, column, row, 1700, 2.4)
    _add_owner(client, space_id, row, "Ольга")
    client.post("/logout", follow_redirects=False)

    pair = f"/novogrudok/{row}/{column}"
    granddaughter = f"{row} — бабушка (мать матери), {column} — внучка"
    swapped = f"{column} — бабушка (мать матери), {row} — внучка"
    anon = client.get(pair, follow_redirects=False)
    assert anon.status_code == 200
    assert "Сохранить связь" not in anon.text
    assert 'action="/novogrudok/kinship"' not in anon.text
    assert 'name="relation"' not in anon.text
    assert KINSHIP_EDITOR_EMAIL not in anon.text

    login(client, "anna", "password123")
    blocked = client.post(
        "/novogrudok/kinship",
        data={"source_kit": row, "match_kit": column, "relation": "mm_f:L"},
        follow_redirects=False,
    )
    assert blocked.status_code == 403
    assert _kinship_count(client) == 0
    client.post("/logout", follow_redirects=False)

    login(client)
    _set_email(client, "admin", KINSHIP_EDITOR_EMAIL)
    saved = client.post(
        "/novogrudok/kinship",
        data={"source_kit": row, "match_kit": column, "relation": "mm_f:L"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    client.post("/logout", follow_redirects=False)
    public = client.get(pair, follow_redirects=False)
    assert public.status_code == 200
    assert f"Сохранённая связь: {granddaughter}. Расстояние в поколениях: 2." in _visible(public.text)
    assert "Ольга" not in public.text
    assert f"{column} (" not in public.text
    assert "Сохранить связь" not in public.text
    assert 'name="relation"' not in public.text

    login(client)
    again = client.post(
        "/novogrudok/kinship",
        data={"source_kit": row, "match_kit": column, "relation": "mm_f:R"},
        follow_redirects=False,
    )
    assert again.status_code == 303
    db = client.app.state.session_factory()
    try:
        rows = list(db.scalars(select(GedMatchKinship).where(GedMatchKinship.space_id == space_id)))
        assert len(rows) == 1
        assert rows[0].relation == "gp_mm_f"
        assert rows[0].parent_kit == column
        assert rows[0].kit_low == row
        assert rows[0].kit_high == column
    finally:
        db.close()
    shown = client.get(pair, follow_redirects=False)
    assert f"Сохранённая связь: {swapped}. Расстояние в поколениях: 2." in _visible(shown.text)
    assert "Ольга" not in shown.text
    forecast = forecast_pair({(row, column): (1700.0, None)}, row, column)
    merged = apply_manual(forecast, "gp_mm_f", column, row, column)
    assert merged["generation_distance"] == 2
    assert "мать матери" not in merged["hypothesis"]["summary"]
    assert "внук или внучка" in merged["hypothesis"]["summary"]


def test_novogrudok_pair_shows_sex_and_keeps_manual_link(client):
    login(client)
    opened = client.post("/admin/spaces", data={"name": "Новогрудок", "add_self": "on"}, follow_redirects=True)
    space_id = find_space_id(opened.text, "Новогрудок")
    assert space_id == 1
    row, column = "T594513", "T631421"
    _add_owner(client, space_id, row, "*Khvojnitskaya Elena")
    _add_owner(client, space_id, column, "*Furs Nina adm. by hvojka")
    _add_hit(client, space_id, row, column, 3600, 1, name="*Furs Nina adm. by hvojka")
    client.post("/logout", follow_redirects=False)

    pair = f"/novogrudok/{row}/{column}"
    anon = client.get(pair, follow_redirects=False)
    assert anon.status_code == 200
    assert f"{row} — мать, {column} — дочь" in _visible(anon.text)
    assert f"{column} — мать, {row} — дочь" in _visible(anon.text)
    assert "*Khvojnitskaya Elena" not in anon.text
    assert "*Furs Nina" not in anon.text
    assert f'href="{_gedmatch_kit(row)}"' in anon.text
    assert f'href="{_gedmatch_kit(column)}"' in anon.text
    assert "сын" not in anon.text
    assert "Сохранить связь" not in anon.text

    login(client)
    _set_email(client, "admin", KINSHIP_EDITOR_EMAIL)
    saved = client.post(
        "/novogrudok/kinship",
        data={"source_kit": row, "match_kit": column, "relation": "unrelated"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    shown = client.get(pair, follow_redirects=False)
    assert f"Сохранённая связь: {row} и {column} — не родственники." in _visible(shown.text)
    hypothesis = _visible(shown.text.split("Гипотеза по cM:", 1)[1].split("</p>", 1)[0])
    assert f"{row} — мать, {column} — дочь" in hypothesis
    assert "*Khvojnitskaya Elena" not in shown.text
    assert "*Furs Nina" not in shown.text
    assert "не родственники" not in hypothesis


def _kit_sex(client, kit: str) -> str:
    db = client.app.state.session_factory()
    try:
        row = db.scalar(select(GedMatchKit).where(GedMatchKit.source_kit == kit))
        return (row.sex or "") if row is not None else ""
    finally:
        db.close()


def _set_kit_sex(client, kit: str, sex: str) -> None:
    db = client.app.state.session_factory()
    row = db.scalar(select(GedMatchKit).where(GedMatchKit.source_kit == kit))
    row.sex = sex
    db.commit()
    db.close()


def test_novogrudok_forecast_block_without_manual_link(client):
    login(client)
    opened = client.post("/admin/spaces", data={"name": "Новогрудок", "add_self": "on"}, follow_redirects=True)
    space_id = find_space_id(opened.text, "Новогрудок")
    assert space_id == 1
    row, column = "ELENA001", "IVAN0001"
    _add_owner(client, space_id, row, "Elena")
    _add_owner(client, space_id, column, "Ivan")
    _add_hit(client, space_id, row, column, 1800, 1.5, name="Ivan")
    client.post("/logout", follow_redirects=False)

    pair = f"/novogrudok/{row}/{column}"
    page = client.get(pair, follow_redirects=False)
    assert page.status_code == 200
    assert "<h2>Прогноз родства</h2>" in page.text
    assert "гипотез" in page.text
    assert "Сохранённая связь" not in page.text
    assert "брат и сестра" in page.text
    assert "Elena" not in page.text
    assert "Ivan" not in page.text
    assert f'href="{_gedmatch_kit(row)}"' in page.text
    assert f'href="{_gedmatch_kit(column)}"' in page.text
    assert _kit_sex(client, row) == ""
    assert _kit_sex(client, column) == ""

    missing = client.get("/novogrudok/NONE0001/NONE0002", follow_redirects=False)
    assert missing.status_code == 200
    assert "<h2>Прогноз родства</h2>" in missing.text
    assert "Эти наборы не сравнивались, прогноз родства не строится." in missing.text
    for word in ("мать", "отец", "дочь", "сын", "брат", "сестра", "бабушка", "дедушка", "внук", "внучка"):
        assert word not in missing.text

    login(client)
    _set_email(client, "admin", KINSHIP_EDITOR_EMAIL)
    saved = client.post(
        "/novogrudok/kinship",
        data={"source_kit": row, "match_kit": column, "relation": "unrelated"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    shown = client.get(pair, follow_redirects=False)
    assert f"Сохранённая связь: {row} и {column} — не родственники." in _visible(shown.text)
    assert "Elena" not in shown.text
    assert "Ivan" not in shown.text
    assert "<h2>Прогноз родства</h2>" in shown.text
    assert "Гипотеза по cM:" in shown.text
    assert _kit_sex(client, row) == ""
    assert _kit_sex(client, column) == ""


def test_stored_kit_sex_overrides_name(client):
    login(client)
    opened = client.post("/admin/spaces", data={"name": "Новогрудок", "add_self": "on"}, follow_redirects=True)
    space_id = find_space_id(opened.text, "Новогрудок")
    row, column = "IVAN0001", "ELENA001"
    _add_owner(client, space_id, row, "Ivan")
    _add_owner(client, space_id, column, "Elena")
    _set_kit_sex(client, row, "F")
    _set_kit_sex(client, column, "M")
    _add_hit(client, space_id, row, column, 3600, 1, name="Elena")
    client.post("/logout", follow_redirects=False)

    page = client.get(f"/novogrudok/{row}/{column}", follow_redirects=False)
    assert page.status_code == 200
    visible = _visible(page.text)
    assert f"{row} — мать, {column} — сын" in visible
    assert f"{column} — отец, {row} — дочь" in visible
    assert f"{row} — мать, {column} — дочь" not in visible
    assert f"{column} — мать, {row} — дочь" not in visible
    assert f"{row} — отец" not in visible
    assert f"{column} — мать" not in visible
    assert "Ivan" not in page.text
    assert "Elena" not in page.text
    assert _kit_sex(client, row) == "F"
    assert _kit_sex(client, column) == "M"


def test_linked_person_sex_is_not_copied_onto_the_kit(client):
    login(client)
    opened = client.post("/admin/spaces", data={"name": "Новогрудок", "add_self": "on"}, follow_redirects=True)
    space_id = find_space_id(opened.text, "Новогрудок")
    row, column = "IVAN0001", "ELENA001"
    _add_owner(client, space_id, row, "Ivan")
    _add_owner(client, space_id, column, "Elena")
    _add_hit(client, space_id, row, column, 3600, 1, name="Elena")
    db = client.app.state.session_factory()
    try:
        tree = Tree(space_id=space_id, filename="tree.ged", stored_path="tree.ged", created_at=utcnow())
        db.add(tree)
        db.flush()
        person = Person(tree_id=tree.id, gedcom_id="I1", full_name="Elena", sex="M")
        db.add(person)
        db.flush()
        hit = db.scalar(
            select(GedMatchHit).where(
                GedMatchHit.space_id == space_id,
                GedMatchHit.source_kit == row,
                GedMatchHit.match_kit == column,
            )
        )
        hit.person_id = person.id
        db.commit()
    finally:
        db.close()
    client.post("/logout", follow_redirects=False)

    page = client.get(f"/novogrudok/{row}/{column}", follow_redirects=False)
    assert page.status_code == 200
    visible = _visible(page.text)
    assert "мать" not in visible
    assert "дочь" not in visible
    assert f"{column} — отец" in visible or f"{column} — сын" in visible
    assert "Elena" not in page.text
    assert "Ivan" not in page.text
    assert _kit_sex(client, column) == ""
    assert _kit_sex(client, row) == ""
