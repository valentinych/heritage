import csv
import io
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.importers.geneteka import GenetekaLoad, build_row, ensure_row, fetch_parish, load_parish
from app.main import create_app
from app.tools.geneteka import upload_rows

FIXTURE = Path(__file__).parent / "fixtures" / "geneteka"
PARISH_URL = "https://geneteka.genealodzy.pl/page1.html?op=gt&lang=pol&bdm=B&w=22br&rid=13826"
LIVE_URL = "https://geneteka.genealodzy.pl/index.php?op=gt&lang=pol&bdm=B&w=22br&rid=13826"

BIRTH_HEAD = (
    "<tr><th>Rok</th><th>Akt</th><th>Imię</th><th>Nazwisko</th><th>Imię ojca</th>"
    "<th>Imię matki</th><th>Nazwisko matki</th><th>Parafia</th><th>Miejscowość</th><th>Uwagi</th></tr>"
)
MARRIAGE_HEAD = (
    "<tr><th>Rok</th><th>Akt</th><th>Imię</th><th>Nazwisko</th><th>Rodzice</th>"
    "<th>Imię</th><th>Nazwisko</th><th>Rodzice</th><th>Parafia</th><th>Uwagi</th></tr>"
)
BIRTH_HTML = f"""
<ul class="tabs">
  <li data-key="b" data-rid="13826"><a href="?op=gt&amp;bdm=B&amp;w=22br&amp;rid=13826">Urodzenia</a></li>
  <li data-key="s" data-rid="13827"><a href="?op=gt&amp;bdm=S&amp;w=22br&amp;rid=13827">Małżeństwa</a></li>
</ul>
<table id="table_b">{BIRTH_HEAD}</table>
"""
MARRIAGE_HTML = f'<table id="table_s">{MARRIAGE_HEAD}</table>'


def _row(**values):
    base = {
        "Событие": "рождение",
        "Год": "1800",
        "Акт": "1",
        "Имя": "Adam",
        "Фамилия": "Bliźniuk",
        "Имя отца": "Jan",
        "Имя матери": "Anna",
        "Фамилия матери": "Nowak",
        "Приход": "Połonka",
        "Место": "Łotwicze",
        "Примечания": "Data urodzenia: 08.12.1800 r.",
        "Адрес": LIVE_URL,
    }
    base.update(values)
    return base


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


def login(client: TestClient):
    response = client.post("/login", data={"username": "admin", "password": "password123"}, follow_redirects=False)
    assert response.status_code == 303


def space_id(client: TestClient) -> int:
    created = client.post("/admin/spaces", data={"name": "Связи", "add_self": "on"}, follow_redirects=True)
    match = re.search(r"<h3>Связи</h3>.*?/admin/spaces/(\d+)/members", created.text, re.S)
    assert match, created.text
    return int(match.group(1))


def test_html_fixture_follows_pages_and_keeps_columns():
    calls = []

    def get(url, ajax=False):
        assert ajax is False
        name = Path(urlparse(url).path).name
        calls.append(name)
        return (FIXTURE / name).read_text(encoding="utf-8")

    loaded = load_parish(PARISH_URL, get, pause=0)
    assert calls == ["page1.html", "page2.html"]
    assert loaded.parish == "Połonka"
    assert loaded.counts() == {"рождение": 3, "брак": 2, "смерть": 0}
    assert "Индекса смертей на странице нет." in loaded.warnings

    births = [row for row in loaded.rows if row["Событие"] == "рождение"]
    adam = next(row for row in births if row["Акт"] == "1")
    assert adam["Фамилия"] == "Bliźniuk (Blizniuk)"
    assert adam["Фамилия матери"] == "Nowak"
    assert adam["Имя отца"] == "Jan"
    assert adam["Место"] == "Łotwicze"
    assert adam["Примечания"] == "Data urodzenia: 08.12.1800 r."
    assert adam["Год рождения"] == "1800"
    assert adam["Фио"] == "Adam Bliźniuk (Blizniuk)"
    assert any(row["Акт"] == "3" and row["Фамилия"] == "Las" for row in births)

    marriages = [row for row in loaded.rows if row["Событие"] == "брак"]
    first = next(row for row in marriages if row["Акт"] == "10")
    assert first["Имя"] == "Aleksiej"
    assert first["Фамилия"] == "Czyran"
    assert first["Родители"] == "ojciec Piotr"
    assert first["Имя супруга"] == "Prakseda"
    assert first["Фамилия супруга"] == "Bryk"
    assert first["Родители супруга"] == "matka Ewa"
    assert first["Место"] == "Horuny"
    assert first["Год"] == "1801"
    assert first["Год рождения"] == ""
    second = next(row for row in marriages if row["Акт"] == "11")
    assert second["Фамилия супруга"] == "Kowal"
    assert second["Родители супруга"] == "matka Anna"


NOWA_MYSZ_BIRTH_HEAD = [
    "Rok",
    "Akt",
    "Imię",
    "Nazwisko",
    "Imię ojca",
    "Imię matki",
    "Nazwisko matki",
    "Parafia",
    "Miejscowość",
    "Uwagi",
]
NOWA_MYSZ_HELENA = [
    1798,
    "12",
    "Helena ",
    "Paszkiewicz",
    "Jakub",
    "Urszula",
    "Ciecierszczanka",
    "Nowa Mysz",
    "Nowomyck",
    (
        '<span class="_i"></span><img src="images/z.png" '
        'title="Miejsce przechowywania ksiąg: &#013;Państwowe Archiwum Historyczne w Wilnie">'
        '<img src="images/a.png" title="Indeks dodał: ArturWilkicki">'
    ),
]


def test_nowa_mysz_birth_api_row_keeps_parents():
    row = build_row(
        "B",
        NOWA_MYSZ_BIRTH_HEAD,
        NOWA_MYSZ_HELENA,
        "https://geneteka.genealodzy.pl/index.php?op=gt&lang=pol&bdm=B&w=22br&rid=3679",
    )
    assert row["Год"] == "1798"
    assert row["Акт"] == "12"
    assert row["Имя"] == "Helena"
    assert row["Фамилия"] == "Paszkiewicz"
    assert row["Имя отца"] == "Jakub"
    assert row["Имя матери"] == "Urszula"
    assert row["Фамилия матери"] == "Ciecierszczanka"
    assert row["Приход"] == "Nowa Mysz"
    assert row["Место"] == "Nowomyck"


def _json_body(url: str) -> str:
    if "getAct.php" not in url:
        if "bdm=S" in url:
            return MARRIAGE_HTML
        return BIRTH_HTML
    query = parse_qs(urlparse(url).query)
    assert query["length"] == ["2"]
    assert query["columns[0][orderable]"] == ["true"]
    assert query["columns[9][orderable]"] == ["false"]
    bdm = query["bdm"][0]
    start = int(query["start"][0])
    if bdm == "S":
        assert start == 0
        return json.dumps({"recordsTotal": 0, "recordsFiltered": 0, "data": [], "parishes": {}})
    assert bdm == "B"
    parish = {"22br": [[13826, "Połonka", "0", ""]]}
    if start == 0:
        data = [
            [1800, "1", "Adam", "Bliźniuk", "Jan", "Anna", "Nowak", "Połonka", "Łotwicze", ""],
            [1800, "2", "Jan", "Test", "Piotr", "Ewa", "Kowal", "Połonka", "Horuny", ""],
        ]
    elif start == 2:
        data = [
            [1800, "1", "Adam", "Bliźniuk", "Jan", "Anna", "Nowak", "Połonka", "Łotwicze", ""],
            [1802, "3", "Ewa", "Las", "", "Anna", "Nowak", "Połonka", "Łotwicze", ""],
        ]
    else:
        raise AssertionError(start)
    return json.dumps({"recordsTotal": 3, "recordsFiltered": 3, "data": data, "parishes": parish})


def test_datatables_pages_are_followed(monkeypatch):
    seen = []

    class Response:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            headers = headers or {}
            seen.append((url, headers))
            return Response(_json_body(url))

    monkeypatch.setattr("app.importers.geneteka.httpx.Client", Client)
    loaded = fetch_parish(LIVE_URL, page_size=2, pause=0)
    act = [(url, headers) for url, headers in seen if "getAct.php" in url]
    assert [int(parse_qs(urlparse(url).query)["start"][0]) for url, _headers in act if "bdm=B" in url] == [0, 2]
    assert sum("bdm=S" in url for url, _headers in act) == 1
    assert all(headers.get("X-Requested-With") == "XMLHttpRequest" for _url, headers in act)
    html_calls = [headers for url, headers in seen if "getAct.php" not in url]
    assert html_calls
    assert all("X-Requested-With" not in headers for headers in html_calls)
    assert loaded.parish == "Połonka"
    assert loaded.counts()["рождение"] == 3
    assert loaded.counts()["брак"] == 0
    assert "Индекса смертей на странице нет." in loaded.warnings
    assert sum(row["Акт"] == "1" for row in loaded.rows) == 1


def test_upload_posts_after_login():
    calls = []

    class Response:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self._payload = payload or {}

        def json(self):
            return self._payload

    class Client:
        def post(self, url, data=None, json=None):
            calls.append((url, data, json))
            if url.endswith("/login"):
                assert data["password"] == "secret"
                return Response(303)
            return Response(200, {"message": "Приход Połonka. Рождения — 1, браки — 0, смерти — 0."})

    message = upload_rows(
        "https://62.238.112.84",
        "admin",
        "secret",
        3,
        {"rows": [_row()]},
        Client(),
    )
    assert [url for url, _data, _json in calls] == [
        "https://62.238.112.84/login",
        "https://62.238.112.84/s/3/links/geneteka",
    ]
    assert calls[1][2]["rows"][0]["Фамилия"] == "Bliźniuk"
    assert "secret" not in message
    assert "Połonka" in message

    class Denied:
        def post(self, url, data=None, json=None):
            return Response(401)

    with pytest.raises(RuntimeError):
        upload_rows("https://62.238.112.84", "admin", "secret", 3, {"rows": []}, Denied())


def test_geneteka_import_requires_login(client):
    response = client.post("/s/1/links/geneteka", json={"rows": [_row()]}, follow_redirects=False)
    assert response.status_code == 303
    assert urlparse(response.headers["location"]).path == "/login"


def test_geneteka_form_and_upload_replace_same_parish(client, monkeypatch):
    login(client)
    space = space_id(client)
    page = client.get(f"/s/{space}/links")
    assert "python -m app.tools.geneteka" in page.text
    assert "--upload https://62.238.112.84" in page.text

    rejected = client.post(
        f"/s/{space}/links/geneteka",
        data={"url": "https://example.com/index.php?op=gt&w=22br&rid=1"},
        follow_redirects=True,
    )
    assert "geneteka.genealodzy.pl" in rejected.text

    payload = {
        "parish": "Połonka",
        "w": "22br",
        "anchor": "13826",
        "warnings": ["Индекса смертей на странице нет."],
        "rows": [
            _row(),
            _row(
                Событие="брак",
                Год="1801",
                Акт="10",
                Имя="Aleksiej",
                Фамилия="Czyran",
                Родители="ojciec Piotr",
                **{"Имя супруга": "Prakseda", "Фамилия супруга": "Bryk", "Родители супруга": "matka Ewa", "Место": "Horuny"},
            ),
        ],
    }
    saved = client.post(f"/s/{space}/links/geneteka", json=payload)
    assert saved.status_code == 200
    assert "Приход Połonka" in saved.json()["message"]
    assert "Рождения — 1" in saved.json()["message"]
    assert "браки — 1" in saved.json()["message"]
    assert "Индекса смертей на странице нет." in saved.json()["message"]

    links = client.get(f"/s/{space}/links")
    assert links.text.count("Geneteka Połonka 22br-13826.csv") == 1
    file_id = re.search(r"/files/datasets/(\d+)", links.text).group(1)
    downloaded = client.get(f"/s/{space}/files/datasets/{file_id}")
    table = list(csv.DictReader(io.StringIO(downloaded.text.lstrip("\ufeff"))))
    birth = next(row for row in table if row["Событие"] == "рождение")
    marriage = next(row for row in table if row["Событие"] == "брак")
    assert birth["Фамилия матери"] == "Nowak"
    assert birth["Год рождения"] == "1800"
    assert marriage["Фамилия супруга"] == "Bryk"
    assert marriage["Родители супруга"] == "matka Ewa"
    assert marriage["Место"] == "Horuny"
    assert marriage["Год"] == "1801"
    assert marriage["Год рождения"] == ""

    again = client.post(
        f"/s/{space}/links/geneteka",
        json={"parish": "Połonka", "w": "22br", "anchor": "13826", "rows": [_row(Акт="9", Имя="Ewa", Фамилия="Las")]},
    )
    assert again.status_code == 200
    assert "Рождения — 1" in again.json()["message"]
    replaced = client.get(f"/s/{space}/links")
    assert replaced.text.count("Geneteka Połonka 22br-13826.csv") == 1
    assert "1 строк" in replaced.text


def test_form_uses_server_fetch(client, monkeypatch):
    login(client)
    space = space_id(client)

    def fake_fetch(url, **kwargs):
        assert "rid=13826" in url
        return GenetekaLoad(
            parish="Połonka",
            w="22br",
            anchor="13826",
            rows=[ensure_row(_row())],
            warnings=[],
        )

    monkeypatch.setattr("app.routes.datasets.fetch_parish", fake_fetch)
    response = client.post(
        f"/s/{space}/links/geneteka",
        data={"url": LIVE_URL},
        follow_redirects=True,
    )
    assert "Приход Połonka" in response.text
    assert "Рождения — 1" in response.text
    assert "Geneteka Połonka 22br-13826.csv" in response.text
