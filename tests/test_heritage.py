import re
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.config import Settings
from app.importers.gedcom import extract, parse_gedcom, read_gedcom
from app.importers.names import extract_year, normalize_name
from app.importers.tables import LINK_FIELDS, parse_float, parse_mapping, read_table
from app.main import create_app

GEDCOM = """\
0 HEAD
1 CHAR UTF-8
0 @I1@ INDI
1 NAME Иван /Иванов/
1 SEX M
1 BIRT
2 DATE 1 JAN 1900
1 FAMS @F1@
0 @I2@ INDI
1 NAME Мария /Петрова/
1 SEX F
1 BIRT
2 DATE 1902
1 FAMS @F1@
0 @I3@ INDI
1 NAME Пётр /Иванов/
1 SEX M
1 BIRT
2 DATE 1925
1 FAMC @F1@
0 @F1@ FAM
1 HUSB @I1@
1 WIFE @I2@
1 CHIL @I3@
0 TRLR
""".encode()

GEDCOM_PETR = """\
0 HEAD
1 CHAR UTF-8
0 @I1@ INDI
1 NAME Петр /Иванов/
1 SEX M
1 BIRT
2 DATE 1925
0 TRLR
""".encode()


class _Form:
    def __init__(self, data):
        self.data = data

    def get(self, key):
        return self.data.get(key)


def location_path(response) -> str:
    parsed = urlparse(response.headers["location"])
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.path}{query}"


def find_space_id(html: str, name: str) -> int:
    match = re.search(rf"<h3>{re.escape(name)}</h3>.*?/admin/spaces/(\d+)/members", html, re.S)
    if match is None:
        match = re.search(rf'<option value="(\d+)"[^>]*>{re.escape(name)}</option>', html)
    assert match, html
    return int(match.group(1))


def find_user_id(html: str, username: str) -> int:
    match = re.search(rf"<h3>{username}</h3>.*?/admin/users/(\d+)/password", html, re.S)
    assert match, html
    return int(match.group(1))


def login(client: TestClient, username: str = "admin", password: str = "password123"):
    response = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return response


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


def _menu_navs(html: str) -> list[str]:
    navs = re.findall(r'<nav class="nav" aria-label="Разделы">.*?</nav>', html, re.S)
    assert navs, html
    return navs


def test_public_home_and_menu(client, monkeypatch):
    monkeypatch.setattr("app.routes.novogrudok.load_column_kits", lambda: ["A1111111"])
    page = client.get("/", follow_redirects=False)
    assert page.status_code == 200
    assert page.headers.get("location") is None
    assert 'action="/login"' not in page.text
    assert "Войти" not in page.text
    assert "Могілкі" in page.text
    assert "ДНК Навагрудка" in page.text
    assert "Города и веси" in page.text
    for nav in _menu_navs(page.text):
        assert re.findall(r'href="([^"]+)"', nav) == ["/mogilki", "/novogrudok", "/vesi", "/register"]
        for hidden in ("GedMatch", "Деревья", "Кластеры", "Связи", "Источники", "/dna", "/gedmatch", "/clusters", "/s/"):
            assert hidden not in nav
    assert 'href="/s/' not in page.text
    assert "/gedmatch" not in page.text

    novogrudok = client.get("/novogrudok", follow_redirects=False)
    assert novogrudok.status_code == 200
    for nav in _menu_navs(novogrudok.text):
        assert re.findall(r'href="([^"]+)"', nav) == ["/mogilki", "/novogrudok", "/vesi", "/register"]
        assert "GedMatch" not in nav
        assert 'href="/s/' not in nav

    locked = client.get("/s/1/gedmatch", follow_redirects=False)
    assert locked.status_code == 303
    assert urlparse(locked.headers["location"]).path == "/login"


def test_names_and_numbers():
    assert normalize_name("  Пётр   Иванов ") == "иванов петр"
    assert extract_year("ABT 1900") == 1900
    assert extract_year("до нашей эры") is None
    assert parse_float("45,6 cM") == 45.6
    assert parse_float("1.234,5") == 1234.5


def test_gedcom_conc_and_encodings():
    people, families = extract(parse_gedcom("0 HEAD\n1 CHAR UTF-8\n0 @I1@ INDI\n1 NAME Иван\n2 CONC  /Иванов/\n1 SEX M\n"))
    assert people[0].full == "Иван Иванов"
    assert families == []
    with pytest.raises(ValueError, match="ANSEL"):
        read_gedcom("0 HEAD\n1 CHAR ANSEL\n0 TRLR\n".encode())
    decoded, _ = read_gedcom("0 HEAD\n1 CHAR UTF-8\n0 @I1@ INDI\n1 NAME Анна /Иванова/\n1 SEX F\n0 TRLR\n".encode("utf-16"))
    assert decoded[0].full == "Анна Иванова"


def test_table_readers_and_mapping(tmp_path):
    csv_path = tmp_path / "people.csv"
    csv_path.write_bytes("Имя;Год\nИван;1900\n".encode("cp1251"))
    headers, rows = read_table(csv_path)
    assert headers == ["Имя", "Год"]
    assert rows == [{"Имя": "Иван", "Год": "1900"}]

    book = Workbook()
    sheet = book.active
    sheet.append(["Имя", "Год"])
    sheet.append(["Анна", 1900])
    xlsx_path = tmp_path / "people.xlsx"
    book.save(xlsx_path)
    headers, rows = read_table(xlsx_path)
    assert rows[0]["Имя"] == "Анна"
    assert rows[0]["Год"] == "1900"

    with pytest.raises(ValueError, match="один раз"):
        parse_mapping(_Form({"name": "Имя", "birth_year": "Имя"}), LINK_FIELDS, ["Имя", "Год"])
    with pytest.raises(ValueError, match="Имя"):
        parse_mapping(_Form({"name": "", "birth_year": "Год"}), LINK_FIELDS, ["Имя", "Год"])


def test_first_login_sets_password(client):
    login(client)
    created = client.post("/admin/users", data={"username": "hvojka"}, follow_redirects=True)
    assert "Пользователь создан." in created.text
    client.post("/logout", follow_redirects=False)

    asked = client.post("/login", data={"username": "hvojka", "password": "anything1"}, follow_redirects=False)
    assert asked.status_code == 200
    assert "Пароль ещё раз" in asked.text

    mismatch = client.post(
        "/login/password",
        data={"username": "hvojka", "password": "new-password", "password_repeat": "other-password"},
        follow_redirects=False,
    )
    assert mismatch.status_code == 400
    assert "Пароли не совпадают." in mismatch.text

    chosen = client.post(
        "/login/password",
        data={"username": "hvojka", "password": "new-password", "password_repeat": "new-password"},
        follow_redirects=False,
    )
    assert chosen.status_code == 303
    client.post("/logout", follow_redirects=False)
    login(client, "hvojka", "new-password")
    assert client.get("/admin", follow_redirects=False).status_code == 404


def test_register_then_login_and_logout(client):
    assert 'href="/register"' in client.get("/").text
    login_page = client.get("/login")
    assert 'href="/register"' in login_page.text
    assert 'href="/register"' not in client.get("/mogilki").text

    form = client.get("/register")
    assert form.status_code == 200
    assert "Регистрация" in form.text
    assert 'action="/register"' in form.text
    assert 'name="email"' not in form.text
    assert "script-src 'none'" in form.headers["content-security-policy"]

    short = client.post("/register", data={"username": "reader", "password": "short"}, follow_redirects=False)
    assert short.status_code == 400
    assert "Пароль короче 8 символов." in short.text

    created = client.post(
        "/register",
        data={"username": "reader", "password": "reader-pass"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert urlparse(created.headers["location"]).path == "/"
    assert client.get("/admin", follow_redirects=False).status_code == 404
    assert client.get("/mogilki/data").status_code == 401

    duplicate = client.post(
        "/register",
        data={"username": "reader", "password": "another-pass"},
        follow_redirects=False,
    )
    assert duplicate.status_code == 400
    assert "Такое имя уже занято." in duplicate.text

    logged_out = client.post("/logout", follow_redirects=False)
    assert logged_out.status_code == 303
    assert urlparse(logged_out.headers["location"]).path == "/login"
    locked = client.get("/admin", follow_redirects=False)
    assert locked.status_code == 303
    assert urlparse(locked.headers["location"]).path == "/login"

    bad = client.post("/login", data={"username": "reader", "password": "wrong-pass"}, follow_redirects=False)
    assert bad.status_code == 401
    assert "Неверное имя или пароль." in bad.text
    missing = client.post("/login", data={"username": "no-such-reader", "password": "wrong-pass"}, follow_redirects=False)
    assert missing.status_code == 401
    assert "Неверное имя или пароль." in missing.text

    login(client, "reader", "reader-pass")
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.get("/admin", follow_redirects=False).status_code == 404


def test_login_logout_and_rate_limit(client):
    page = client.get("/login")
    assert page.status_code == 200
    assert "Войти" in page.text
    bad = client.post("/login", data={"username": "admin", "password": "wrong-pass"}, follow_redirects=False)
    assert bad.status_code == 401
    assert "Неверное имя или пароль." in bad.text

    login(client)
    admin = client.get("/admin")
    assert admin.status_code == 200
    assert "Создать пространство" in admin.text

    client.app.state.login_guard.limit = 2
    client.post("/logout", follow_redirects=False)
    assert client.post("/login", data={"username": "admin", "password": "bad-password-1"}, follow_redirects=False).status_code == 401
    assert client.post("/login", data={"username": "admin", "password": "bad-password-2"}, follow_redirects=False).status_code == 401
    blocked = client.post("/login", data={"username": "admin", "password": "bad-password-3"}, follow_redirects=False)
    assert blocked.status_code == 429
    assert "Слишком много попыток" in blocked.text


def test_tree_card_search_and_delete(client):
    login(client)
    created = client.post("/admin/spaces", data={"name": "Семья", "add_self": "on"}, follow_redirects=True)
    space_id = find_space_id(created.text, "Семья")
    uploaded = client.post(
        f"/s/{space_id}/trees",
        files={"file": ("family.ged", GEDCOM, "application/octet-stream")},
        follow_redirects=False,
    )
    assert uploaded.status_code == 303, uploaded.text
    tree_path = location_path(uploaded)
    tree_page = client.get(tree_path)
    assert "Иван Иванов" in tree_page.text
    assert "Дерево загружено." in tree_page.text

    found = client.get(f"{tree_path}?q=петр")
    person_id = int(re.search(r'href="/s/\d+/people/(\d+)" aria-label="Пётр Иванов', found.text).group(1))
    card = client.get(f"/s/{space_id}/people/{person_id}")
    assert "Иван Иванов" in card.text
    assert "Мария Петрова" in card.text
    assert "мужчина" in card.text

    ivan_id = int(re.search(r'href="/s/\d+/people/(\d+)" aria-label="Иван Иванов', tree_page.text).group(1))
    ivan = client.get(f"/s/{space_id}/people/{ivan_id}")
    assert "В файле нет родителей." in ivan.text
    assert "Мария Петрова" in ivan.text
    assert "Пётр Иванов" in ivan.text

    download = client.get(f"/s/{space_id}/files/trees/{tree_path.rsplit('/', 1)[-1]}")
    assert download.status_code == 200
    assert b"INDI" in download.content

    rejected = client.post(
        f"/s/{space_id}/trees",
        files={"file": ("notes.txt", b"hello", "text/plain")},
        follow_redirects=False,
    )
    assert rejected.status_code == 400
    assert "GEDCOM" in rejected.text

    removed = client.post(f"{tree_path}/delete", follow_redirects=True)
    assert "Пока нет деревьев" in removed.text
    assert client.get(f"/s/{space_id}/people/{person_id}").status_code == 404


def test_cluster_tree_columns():
    from types import SimpleNamespace

    from app.services.cluster_tree import COL_W, GAP, PAD, layout_cluster_tree

    groups = [
        {"label": "1", "members": [SimpleNamespace(name="Анна", shared_cm=40)]},
        {"label": "2", "members": [SimpleNamespace(name="Вера", shared_cm=10)]},
    ]
    tree = layout_cluster_tree(groups, "Семья")
    clusters = [node for node in tree["nodes"] if node["kind"] == "cluster"]
    people = [node for node in tree["nodes"] if node["kind"] == "person"]
    assert clusters[1]["x"] == PAD + COL_W + GAP
    assert clusters[0]["y"] == clusters[1]["y"]
    assert people[0]["y"] > clusters[0]["y"] + clusters[0]["h"]
    assert any("H" in path["d"] for path in tree["paths"] if path["kind"] == "bus")
    assert all(node["sex"] == "" for node in tree["nodes"])


def test_pedigree_puts_child_below_parents():
    from app.services.pedigree import layout_pedigree

    scheme = layout_pedigree(
        [
            {"id": 1, "name": "Иван Иванов", "detail": "р. 1900", "href": "/s/1/people/1", "sex": "M"},
            {"id": 2, "name": "Мария Петрова", "detail": "р. 1902", "href": "/s/1/people/2", "sex": "F"},
            {"id": 3, "name": "Пётр Иванов", "detail": "р. 1925", "href": "/s/1/people/3"},
        ],
        [{"husband_id": 1, "wife_id": 2, "child_ids": [3]}],
    )
    nodes = {node["lines"][0]["full"]: node for node in scheme["nodes"]}
    ivan = nodes["Иван Иванов"]
    maria = nodes["Мария Петрова"]
    petr = nodes["Пётр Иванов"]
    assert ivan["x"] == maria["x"] > petr["x"]
    mid = (ivan["y"] + maria["y"]) / 2
    assert abs(mid - petr["y"]) < ivan["h"] + maria["h"]
    assert scheme["width"] < 1000
    assert any(path["kind"] == "spouse" for path in scheme["paths"])
    assert sum(path["kind"] == "link" for path in scheme["paths"]) == 2
    assert ivan["sex"] == "M"
    assert maria["sex"] == "F"
    assert petr["sex"] == ""


def test_pedigree_keeps_spouses_together_when_both_have_parents():
    from app.services.pedigree import layout_pedigree

    scheme = layout_pedigree(
        [
            {"id": 1, "name": "Дед", "detail": "", "href": ""},
            {"id": 2, "name": "Бабка", "detail": "", "href": ""},
            {"id": 3, "name": "Отец", "detail": "", "href": ""},
            {"id": 4, "name": "Мать", "detail": "", "href": ""},
            {"id": 5, "name": "Дед2", "detail": "", "href": ""},
            {"id": 6, "name": "Бабка2", "detail": "", "href": ""},
            {"id": 7, "name": "Сын", "detail": "", "href": ""},
        ],
        [
            {"husband_id": 1, "wife_id": 2, "child_ids": [3]},
            {"husband_id": 5, "wife_id": 6, "child_ids": [4]},
            {"husband_id": 3, "wife_id": 4, "child_ids": [7]},
        ],
    )
    nodes = {node["lines"][0]["full"]: node for node in scheme["nodes"]}
    father = nodes["Отец"]
    mother = nodes["Мать"]
    son = nodes["Сын"]
    assert father["x"] == mother["x"]
    assert son["x"] < father["x"] < nodes["Дед"]["x"]
    assert mother["x"] < nodes["Дед2"]["x"]
    top, bottom = sorted((father, mother), key=lambda node: node["y"])
    assert bottom["y"] - (top["y"] + top["h"]) < 24
    assert scheme["width"] < 1000
    ded_rail = _link_verticals(scheme, {"Дед", "Бабка"})
    other_rail = _link_verticals(scheme, {"Дед2", "Бабка2"})
    for left in ded_rail:
        for right in other_rail:
            gap = max(left[1], right[1]) - min(left[2], right[2])
            shares_y = gap < 8
            assert not shares_y or abs(left[0] - right[0]) >= 16
    ded_colors = _link_classes(scheme, {"Дед", "Бабка"})
    other_colors = _link_classes(scheme, {"Дед2", "Бабка2"})
    assert ded_colors and other_colors and ded_colors.isdisjoint(other_colors)


def test_pedigree_stacks_uneven_spouses():
    from app.services.pedigree import layout_pedigree

    scheme = layout_pedigree(
        [
            {"id": 1, "name": "Руслан Огородник", "detail": "р. 1988", "href": "", "sex": "M"},
            {"id": 2, "name": "Валентин Огородник", "detail": "1965–1989", "href": "", "sex": "M"},
            {"id": 3, "name": "Марина Пашкевич", "detail": "р. 1965", "href": "", "sex": "F"},
            {"id": 4, "name": "Иван Огородник", "detail": "", "href": "", "sex": "M"},
            {"id": 5, "name": "Лидия", "detail": "", "href": "", "sex": "F"},
            {"id": 6, "name": "Казимир Пашкевич", "detail": "1928–1983", "href": "", "sex": "M"},
            {"id": 7, "name": "Любовь Ждан", "detail": "", "href": "", "sex": "F"},
            {"id": 8, "name": "Дед Ждан", "detail": "", "href": "", "sex": "M"},
        ],
        [
            {"husband_id": 4, "wife_id": 5, "child_ids": [2]},
            {"husband_id": 8, "wife_id": None, "child_ids": [7]},
            {"husband_id": 6, "wife_id": 7, "child_ids": [3]},
            {"husband_id": 2, "wife_id": 3, "child_ids": [1]},
        ],
    )
    nodes = {node["lines"][0]["full"]: node for node in scheme["nodes"]}
    father = nodes["Валентин Огородник"]
    mother = nodes["Марина Пашкевич"]
    assert father["x"] == mother["x"]
    top, bottom = sorted((father, mother), key=lambda node: node["y"])
    assert bottom["y"] - (top["y"] + top["h"]) < 24
    assert nodes["Иван Огородник"]["x"] > father["x"]
    assert nodes["Казимир Пашкевич"]["x"] > mother["x"]


def _link_verticals(scheme, parent_names):
    nodes = scheme["nodes"]

    def person(x, y, side):
        for node in nodes:
            center = node["y"] + node["h"] / 2
            edge = node["x"] + node["w"] if side == "right" else node["x"]
            if abs(edge - x) < 1.5 and abs(center - y) < 1.5:
                return node["lines"][0]["full"]
        return ""

    found = []
    for path in scheme["paths"]:
        if path["kind"] != "link":
            continue
        parts = path["d"].split()
        xs, ys = [], []
        index = 0
        while index < len(parts):
            cmd = parts[index]
            if cmd == "M":
                xs.append(float(parts[index + 1]))
                ys.append(float(parts[index + 2]))
                index += 3
            elif cmd == "H":
                xs.append(float(parts[index + 1]))
                index += 2
            elif cmd == "V":
                ys.append(float(parts[index + 1]))
                index += 2
            else:
                break
        if len(xs) < 2 or len(ys) < 2 or abs(ys[0] - ys[-1]) < 1:
            continue
        ends = {person(xs[0], ys[0], "right"), person(xs[-1], ys[-1], "left")}
        if ends & parent_names:
            found.append((xs[1], min(ys[0], ys[-1]), max(ys[0], ys[-1])))
    return found


def _link_classes(scheme, parent_names):
    nodes = scheme["nodes"]

    def person(x, y, side):
        for node in nodes:
            center = node["y"] + node["h"] / 2
            edge = node["x"] + node["w"] if side == "right" else node["x"]
            if abs(edge - x) < 1.5 and abs(center - y) < 1.5:
                return node["lines"][0]["full"]
        return ""

    found = set()
    for path in scheme["paths"]:
        if path["kind"] != "link":
            continue
        parts = path["d"].split()
        xs, ys = [], []
        index = 0
        while index < len(parts):
            cmd = parts[index]
            if cmd == "M":
                xs.append(float(parts[index + 1]))
                ys.append(float(parts[index + 2]))
                index += 3
            elif cmd == "H":
                xs.append(float(parts[index + 1]))
                index += 2
            elif cmd == "V":
                ys.append(float(parts[index + 1]))
                index += 2
            else:
                break
        if len(xs) < 2 or len(ys) < 2:
            continue
        ends = {person(xs[0], ys[0], "right"), person(xs[-1], ys[-1], "left")}
        if ends & parent_names:
            found.add(path["css"])
    return found


def test_cluster_branches_follow_matrix_rules():
    from types import SimpleNamespace

    from app.services.cluster_branches import analyze_autocluster, build_branches

    def member(name, cm=10):
        return SimpleNamespace(name=name, shared_cm=cm)

    headers = ["Cluster", "Name", "1_A", "2_B", "3_C"]
    records = [
        {"Cluster": "1", "Name": "A", "1_A": "", "2_B": "2", "3_C": ""},
        {"Cluster": "2", "Name": "B", "1_A": "", "2_B": "", "3_C": "3"},
        {"Cluster": "3", "Name": "C", "1_A": "1", "2_B": "", "3_C": ""},
    ]
    analysis = analyze_autocluster(headers, records)
    assert {("1", "2"), ("1", "3"), ("2", "3")} <= analysis["cross"]

    groups = [
        {"label": "1", "members": [member("Ann"), member("Bob")]},
        {"label": "4", "members": [member("C")]},
        {"label": "9", "members": [member("D")]},
        {"label": "11", "members": [member("E")]},
        {"label": "14", "members": [member("F"), member("G"), member("H")]},
        {
            "label": "2",
            "members": [member("Maria Ciechanowska"), member("Grzegorz Ciechanowski"), member("Vlad")],
        },
    ]
    built = {
        "stats": {
            "1": {"count": 1, "possible": 1, "holder": None, "pairs": set(), "names": ["Ann", "Bob"]},
            "4": {"count": 0, "possible": 0, "holder": None, "pairs": set(), "names": ["C"]},
            "9": {"count": 0, "possible": 0, "holder": None, "pairs": set(), "names": ["D"]},
            "11": {"count": 0, "possible": 0, "holder": None, "pairs": set(), "names": ["E"]},
            "14": {
                "count": 3,
                "possible": 3,
                "holder": None,
                "pairs": {frozenset(pair) for pair in (("F", "G"), ("F", "H"), ("G", "H"))},
                "names": ["F", "G", "H"],
            },
            "2": {
                "count": 1,
                "possible": 3,
                "holder": "Vlad",
                "pairs": {frozenset(("Maria Ciechanowska", "Grzegorz Ciechanowski"))},
                "names": ["Maria Ciechanowska", "Grzegorz Ciechanowski", "Vlad"],
            },
        },
        "cross": {("4", "9"), ("4", "11"), ("9", "11"), ("1", "16")},
    }
    branches = build_branches(groups, built)
    medium = [branch for branch in branches if branch["kind"] == "branch"]
    assert [column["lines"][0][2] for column in medium[0]["columns"]] == ["Кластер 4", "Кластер 9", "Кластер 11"]
    assert medium[0]["lines"][0][2] == "Ветвь A"
    assert medium[1]["columns"][0]["lines"][0][2] == "Кластер 14"
    assert [column["lines"][0][2] for column in medium[2]["columns"]] == ["под предком пары", "не под предком пары"]
    twigs = [branch for branch in branches if branch["kind"] == "twig"]
    assert any(branch["columns"][0]["lines"][0][2] == "Кластер 1" for branch in twigs)


def test_matches_and_clusters(client):
    login(client)
    created = client.post("/admin/spaces", data={"name": "ДНК", "add_self": "on"}, follow_redirects=True)
    space_id = find_space_id(created.text, "ДНК")

    uploaded = client.post(
        f"/s/{space_id}/dna",
        files={"file": ("matches.csv", "Имя,Общий cM,Сегмент\nАнна,55.5,20\n".encode(), "text/csv")},
        follow_redirects=False,
    )
    saved = client.post(
        location_path(uploaded),
        data={"name": "Имя", "shared_cm": "Общий cM", "largest_segment": "Сегмент"},
        follow_redirects=True,
    )
    assert "Анна" in saved.text
    assert "55.5" in saved.text
    assert "Максимум общего cM: 55.5" in saved.text

    clustered = client.post(
        f"/s/{space_id}/clusters",
        files={"file": ("clusters.csv", "Кластер;Имя;cM\n1;Анна;40\n1;Борис;30\n2;Вера;10\n".encode(), "text/csv")},
        follow_redirects=False,
    )
    page = client.post(
        location_path(clustered),
        data={"cluster": "Кластер", "name": "Имя", "shared_cm": "cM"},
        follow_redirects=True,
    )
    assert "Кластер 1" in page.text
    assert "Кластер 2" in page.text
    assert "Вера" in page.text
    assert "40 cM" in page.text
    assert 'class="tree-bus"' in page.text
    assert 'class="node node-root"' in page.text


def test_links_confirm_and_reject(client):
    login(client)
    created = client.post("/admin/spaces", data={"name": "Связи", "add_self": "on"}, follow_redirects=True)
    space_id = find_space_id(created.text, "Связи")
    client.post(
        f"/s/{space_id}/trees",
        files={"file": ("family.ged", GEDCOM, "application/octet-stream")},
        follow_redirects=False,
    )
    client.post(
        f"/s/{space_id}/trees",
        files={"file": ("petr.ged", GEDCOM_PETR, "application/octet-stream")},
        follow_redirects=False,
    )
    uploaded = client.post(
        f"/s/{space_id}/links",
        files={"file": ("links.csv", "Имя;Год\nПетр Иванов;1925\nМария Петрова;1902\n".encode(), "text/csv")},
        follow_redirects=False,
    )
    mapped = client.post(
        location_path(uploaded),
        data={"name": "Имя", "birth_year": "Год"},
        follow_redirects=True,
    )
    assert "Новые пары — 1" in mapped.text
    assert "Мария Петрова, 1902 — один человек" in mapped.text
    assert "Найти совпадения" in mapped.text

    link_id = int(re.search(r"/links/(\d+)/status", mapped.text).group(1))
    confirmed = client.post(
        f"/s/{space_id}/links/{link_id}/status",
        data={"status": "confirmed", "back": "proposed"},
        follow_redirects=True,
    )
    assert "Связь подтверждена." in confirmed.text
    confirmed_page = client.get(f"/s/{space_id}/links?status=confirmed")
    assert "Пётр Иванов" in confirmed_page.text
    assert "Петр Иванов" in confirmed_page.text
    person_path = re.search(r'href="(/s/\d+/people/\d+)"', confirmed_page.text).group(1)
    person = client.get(person_path)
    assert "family.ged" in person.text
    assert "petr.ged" in person.text

    client.post(
        f"/s/{space_id}/links/{link_id}/status",
        data={"status": "rejected", "back": "rejected"},
        follow_redirects=False,
    )
    dataset_match = re.search(r"/links/(\d+)/match", mapped.text)
    client.post(f"/s/{space_id}/links/{dataset_match.group(1)}/match", follow_redirects=True)
    proposed = client.get(f"/s/{space_id}/links?status=proposed")
    rejected = client.get(f"/s/{space_id}/links?status=rejected")
    assert f"/links/{link_id}/status" not in proposed.text
    assert f"/links/{link_id}/status" in rejected.text
    assert "отклонено" in rejected.text


def test_membership_hides_spaces(client):
    login(client)
    admin_page = client.post(
        "/admin/users",
        data={"username": "anna", "password": "password123"},
        follow_redirects=True,
    )
    assert "Пользователь создан." in admin_page.text
    anna_id = find_user_id(admin_page.text, "anna")
    admin_id = find_user_id(admin_page.text, "admin")

    denied = client.post(f"/admin/users/{admin_id}/delete", follow_redirects=False)
    assert denied.status_code == 400
    assert "Нельзя удалить свою" in denied.text

    opened = client.post("/admin/spaces", data={"name": "Семья", "add_self": "on"}, follow_redirects=True)
    family_id = find_space_id(opened.text, "Семья")
    closed = client.post("/admin/spaces", data={"name": "Закрытое"}, follow_redirects=True)
    closed_id = find_space_id(closed.text, "Закрытое")
    assert client.get(f"/s/{closed_id}").status_code == 404

    client.post(
        f"/s/{family_id}/trees",
        files={"file": ("family.ged", GEDCOM, "application/octet-stream")},
        follow_redirects=False,
    )
    client.post("/logout", follow_redirects=False)
    login(client, "anna", "password123")
    home = client.get("/")
    assert home.status_code == 200
    assert "Семья" not in home.text
    assert client.get(f"/s/{family_id}/trees").status_code == 404
    assert client.get("/admin").status_code == 404

    client.post("/logout", follow_redirects=False)
    login(client)
    client.post(
        f"/admin/spaces/{family_id}/members",
        content=f"user_id={admin_id}&user_id={anna_id}".encode(),
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    changed = client.post(
        f"/admin/users/{anna_id}/password",
        data={"password": "another-password"},
        follow_redirects=True,
    )
    assert "Пароль обновлён." in changed.text
    client.post("/logout", follow_redirects=False)
    login(client, "anna", "another-password")
    assert client.get(f"/s/{family_id}/trees").status_code == 200
    assert client.get(f"/s/{closed_id}/trees").status_code == 404
    tree_page = client.get(f"/s/{family_id}/trees")
    tree_id = int(re.search(r"/trees/(\d+)", tree_page.text).group(1))
    assert b"INDI" in client.get(f"/s/{family_id}/files/trees/{tree_id}").content
    sources = client.get(f"/s/{family_id}/sources")
    assert sources.status_code == 200
    assert "Фильтр ещё не задан" in sources.text
