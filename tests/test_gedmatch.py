import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings
from app.db import create_tables
from app.importers.gedmatch import parse_kit_owner, parse_one_to_many, rank_kit, walk_rank
from app.main import create_app
from app.deps import KINSHIP_EDITOR_EMAIL
from app.models import GedMatchHit, GedMatchKinship, GedMatchKit, GedMatchMatrix, User
from app.models import GedMatchMatrixKit
from app.services.gedmatch_predict import (
    apply_manual,
    assign_matrix_kit,
    forecast_pair,
    gen_tier_class,
    kinship_form_options,
    kits_for_matrix,
    list_matrices,
    relationship_labels,
    sex_from_name,
    split_loose_clusters,
    strong_clusters,
)
from tests.test_heritage import GEDCOM, find_space_id, find_user_id, location_path, login


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

HTML = """
<p>Kit: BX7602122 (Anna Tester, owner@example.com) [MyHeritage]</p>
<table>
  <tr><th>Kit</th><th>Name</th><th>Email</th><th>Largest Seg</th><th>Total cM</th><th>Gen</th><th>Overlap</th><th>Date Compared</th><th>Source</th></tr>
  <tr><td>A1111111</td><td>Anna Pashkevich</td><td>secret@example.com</td><td>40.2</td><td>180.5</td><td>3.2</td><td>900</td><td>2026-09-20</td><td>MyHeritage</td></tr>
  <tr><td>B2222222</td><td>Иван Иванов</td><td></td><td>12</td><td>48</td><td>4</td><td>400</td><td>2026-09-21</td><td>FTDNA</td></tr>
  <tr><td>A1111111</td><td>повтор</td><td></td><td>1</td><td>1</td><td>1</td><td>1</td><td></td></tr>
</table>
"""


def test_parse_one_to_many_skips_email_and_duplicates():
    rows = parse_one_to_many(HTML)
    assert [row["kit"] for row in rows] == ["A1111111", "B2222222"]
    assert rows[0]["name"] == "Anna Pashkevich"
    assert rows[0]["total_cm"] == 180.5
    assert rows[0]["company"] == "MyHeritage"
    assert rows[0]["largest_cm"] == 40.2
    assert rows[0]["generations"] == 3.2
    assert rows[0]["overlap"] == 900
    assert rows[0]["date_compared"] == "2026-09-20"
    assert "email" not in rows[0]
    assert "secret@example.com" not in str(rows[0])


def test_parse_kit_owner_header():
    owner = parse_kit_owner(HTML)
    assert owner["kit"] == "BX7602122"
    assert owner["name"] == "Anna Tester"
    assert owner["email"] == "owner@example.com"
    assert owner["company"] == "MyHeritage"
    assert parse_kit_owner("<table></table>") is None


def test_relationship_labels_overlap():
    assert "родитель или ребёнок" in relationship_labels(3000)
    assert "брат или сестра" in relationship_labels(3000)
    assert relationship_labels(None) == []
    grand = relationship_labels(1700)
    assert any("внук или внучка" in label for label in grand)
    assert all("мать матери" not in label for label in grand)


def test_gedmatch_page_matches_tree_and_cluster(client):
    login(client)
    created = client.post("/admin/spaces", data={"name": "Семья", "add_self": "on"}, follow_redirects=True)
    space_id = find_space_id(created.text, "Семья")
    client.post(
        f"/s/{space_id}/trees",
        files={"file": ("family.ged", GEDCOM, "application/octet-stream")},
        follow_redirects=False,
    )
    clustered = client.post(
        f"/s/{space_id}/clusters",
        data={"owner_other": "Руслан"},
        files={"file": ("clusters.csv", "cluster,name\n1,Anna Pashkevich\n1,Иван Иванов\n".encode(), "text/csv")},
        follow_redirects=False,
    )
    client.post(
        location_path(clustered),
        data={"cluster": "cluster", "name": "name"},
        follow_redirects=False,
    )
    page = client.get(f"/s/{space_id}/gedmatch")
    assert page.status_code == 200
    assert "GedMatch" in page.text
    uploaded = client.post(
        f"/s/{space_id}/gedmatch",
        data={"kit": "BX7602122"},
        files={"file": ("one.html", HTML.encode(), "text/html")},
        follow_redirects=True,
    )
    assert "Сохранено совпадений: 2." in uploaded.text
    assert "gedmatch-table" not in uploaded.text
    assert "Anna Pashkevich" not in uploaded.text
    assert "Показать таблицу" in uploaded.text
    shown = client.get(f"/s/{space_id}/gedmatch?kit=BX7602122&show=1")
    assert "Anna Pashkevich" in shown.text
    assert "secret@example.com" not in uploaded.text
    assert "secret@example.com" not in shown.text
    assert "кластер 1" in shown.text
    assert "Иван Иванов" in shown.text
    assert "двоюродный" in shown.text
    assert "gedmatch-table" in shown.text
    assert "gedmatch-link" in shown.text
    assert "Источник" in shown.text
    assert "Крупнейший сегмент" in shown.text
    assert "Поколения" in shown.text
    assert "Перекрытие" in shown.text
    assert "Дата сравнения" in shown.text
    assert "2026-09-20" in shown.text
    assert "В деревьях" not in shown.text
    assert "В кластерах" not in shown.text
    assert "<summary>Tester</summary>" in shown.text
    assert "owner@example.com" in shown.text
    assert "BX7602122 · MyHeritage" in shown.text
    assert "gedmatch-matrix" in shown.text


def test_rank_kit_skips_own_kit():
    assert rank_kit(["AA111111", "BB222222", "CC333333"], "AA111111", 1) == "BB222222"
    assert rank_kit(["AA111111", "BB222222"], "AA111111", 2) is None


def test_walk_rank_stops_when_next_page_saved():
    pages = {"BX7602122": ["WP6609474", "BZ5003907"], "WP6609474": ["BX7602122"]}
    saved = {"WP6609474"}
    order, opened, skipped = walk_rank(pages, "BX7602122", 1, saved)
    assert order == ["BX7602122"]
    assert opened == ["BX7602122"]
    assert skipped == []
    assert "WP6609474" not in opened
    assert saved == {"BX7602122", "WP6609474"}


def test_matrix_cells_are_one_direction(client):
    login(client)
    created = client.post("/admin/spaces", data={"name": "Матрица", "add_self": "on"}, follow_redirects=True)
    space_id = find_space_id(created.text, "Матрица")
    other = """
<p>Kit: A1111111 (Other Person, other@example.com) [FTDNA]</p>
<table>
  <tr><th>Kit</th><th>Name</th><th>Email</th><th>Total cM</th><th>Date Compared</th><th>Source</th></tr>
  <tr><td>BX7602122</td><td>Anna Tester</td><td>hidden@example.com</td><td>90</td><td>2026-09-22</td><td>MyHeritage</td></tr>
</table>
"""
    client.post(
        f"/s/{space_id}/gedmatch",
        data={"kit": "BX7602122"},
        files={"file": ("one.html", HTML.encode(), "text/html")},
        follow_redirects=True,
    )
    landed = client.post(
        f"/s/{space_id}/gedmatch",
        data={"kit": "A1111111"},
        files={"file": ("two.html", other.encode(), "text/html")},
        follow_redirects=True,
    )
    assert "gedmatch-matrix" not in landed.text
    page = client.get(f"/s/{space_id}/gedmatch?kit=A1111111&show=1")
    assert "gedmatch-matrix" in page.text
    assert "gedmatch-matrix-col" in page.text
    assert "gedmatch-matrix-self" in page.text
    assert "180.5" in page.text
    assert "gedmatch-gen-4" in page.text
    assert ">90<" in page.text
    assert "hidden@example.com" not in page.text
    assert "B2222222" not in page.text[page.text.find("gedmatch-matrix"):page.text.find("gedmatch-table")]
    row_headers = [
        part.split("</th>", 1)[0]
        for part in page.text.split('<th scope="row" class="kit">')[1:]
    ]
    bx = next(header for header in row_headers if "BX7602122" in header)
    assert "Anna Tester" in bx
    assert "owner@example.com" not in bx
    other = next(header for header in row_headers if "A1111111" in header)
    assert "Other Person" in other
    assert "other@example.com" not in other
    assert "hidden@example.com" not in "".join(row_headers)


def test_strong_clusters_join_one_direction_only():
    clusters = strong_clusters(
        ["A1111111", "BX7602122", "C3333333"],
        {
            ("BX7602122", "A1111111"): 180.5,
            ("C3333333", "BX7602122"): 10.0,
        },
    )
    assert clusters == [["A1111111", "BX7602122"], ["C3333333"]]
    assert strong_clusters(["B", "A"], {("B", "A"): 46}) == [["A", "B"]]
    assert strong_clusters(["B", "A"], {("B", "A"): 45.9}) == [["A"], ["B"]]


def test_cluster_matrix_cell_opens_stored_match(client):
    login(client)
    created = client.post("/admin/spaces", data={"name": "Кластеры китов", "add_self": "on"}, follow_redirects=True)
    space_id = find_space_id(created.text, "Кластеры китов")
    opened = """
<p>Kit: A1111111 (Other Person, other@example.com) [FTDNA]</p>
<table>
  <tr><th>Kit</th><th>Name</th><th>Email</th><th>Largest Seg</th><th>Total cM</th><th>Gen</th><th>Overlap</th><th>Date Compared</th><th>Source</th></tr>
  <tr><td>ZZ999999</td><td>Closed Kit</td><td>hidden@example.com</td><td>8</td><td>12</td><td>5</td><td>100</td><td>2026-09-22</td><td>FTDNA</td></tr>
</table>
"""
    client.post(
        f"/s/{space_id}/gedmatch",
        data={"kit": "BX7602122"},
        files={"file": ("one.html", HTML.encode(), "text/html")},
        follow_redirects=True,
    )
    listed = client.post(
        f"/s/{space_id}/gedmatch",
        data={"kit": "A1111111"},
        files={"file": ("two.html", opened.encode(), "text/html")},
        follow_redirects=True,
    )
    assert 'href="/s/' + str(space_id) + '/gedmatch/clusters"' in listed.text
    matrix = client.get(f"/s/{space_id}/gedmatch/clusters")
    assert matrix.status_code == 200
    assert "onclick" not in matrix.text
    assert "secret@example.com" not in matrix.text
    assert "owner@example.com" not in matrix.text
    assert "other@example.com" not in matrix.text
    assert "hidden@example.com" not in matrix.text
    assert "B2222222" not in matrix.text
    assert "ZZ999999" not in matrix.text
    href = f"/s/{space_id}/gedmatch/clusters/BX7602122/A1111111"
    assert f'<td class="ac-h1"><a href="{href}">3.2</a></td>' in matrix.text
    assert "180.5" not in matrix.text
    assert f"/s/{space_id}/gedmatch/clusters/A1111111/BX7602122" not in matrix.text
    detail = client.get(href)
    assert detail.status_code == 200
    assert "180.5" in detail.text
    assert "40.2" in detail.text
    assert "3.2" in detail.text
    assert "900" in detail.text
    assert "2026-09-20" in detail.text
    assert "MyHeritage" in detail.text
    assert "BX7602122" in detail.text
    assert "A1111111" in detail.text
    assert "secret@example.com" not in detail.text
    assert "owner@example.com" not in detail.text
    assert "other@example.com" not in detail.text
    missing = client.get(f"/s/{space_id}/gedmatch/clusters/A1111111/BX7602122")
    assert missing.status_code == 404


def test_named_matrix_reuses_stored_hits(client):
    login(client)
    created = client.post("/admin/spaces", data={"name": "Матрицы", "add_self": "on"}, follow_redirects=True)
    space_id = find_space_id(created.text, "Матрицы")
    assert space_id == 1
    other = """
<p>Kit: A1111111 (Other Person, other@example.com) [FTDNA]</p>
<table>
  <tr><th>Kit</th><th>Name</th><th>Email</th><th>Total cM</th><th>Date Compared</th><th>Source</th></tr>
  <tr><td>BX7602122</td><td>Anna Tester</td><td>hidden@example.com</td><td>90</td><td>2026-09-22</td><td>MyHeritage</td></tr>
</table>
"""
    client.post(
        f"/s/{space_id}/gedmatch",
        data={"kit": "BX7602122"},
        files={"file": ("one.html", HTML.encode(), "text/html")},
        follow_redirects=True,
    )
    client.post(
        f"/s/{space_id}/gedmatch",
        data={"kit": "A1111111"},
        files={"file": ("two.html", other.encode(), "text/html")},
        follow_redirects=True,
    )
    db = client.app.state.session_factory()
    try:
        before = db.scalar(select(func.count()).select_from(GedMatchHit).where(GedMatchHit.space_id == space_id))
    finally:
        db.close()
    create_tables(client.app.state.engine)
    create_tables(client.app.state.engine)
    page = client.get(f"/s/{space_id}/gedmatch")
    assert "Ruslan Aharodnik" in page.text
    db = client.app.state.session_factory()
    try:
        seeded = db.scalar(
            select(func.count())
            .select_from(GedMatchMatrix)
            .where(GedMatchMatrix.space_id == space_id, GedMatchMatrix.name == "Ruslan Aharodnik")
        )
    finally:
        db.close()
    assert seeded == 1
    empty = client.post(f"/s/{space_id}/gedmatch/matrices", data={"name": "   "}, follow_redirects=True)
    assert "Укажите имя матрицы." in empty.text
    made = client.post(
        f"/s/{space_id}/gedmatch/matrices",
        data={"name": "Вторая линия", "kit": "A1111111"},
        follow_redirects=True,
    )
    assert "Вторая линия" in made.text
    duplicate = client.post(
        f"/s/{space_id}/gedmatch/matrices",
        data={"name": "Вторая линия", "kit": "A1111111"},
        follow_redirects=True,
    )
    assert "Матрица с таким именем уже есть." in duplicate.text
    db = client.app.state.session_factory()
    try:
        after = db.scalar(select(func.count()).select_from(GedMatchHit).where(GedMatchHit.space_id == space_id))
        matrix_id = db.scalar(
            select(GedMatchMatrix.id).where(
                GedMatchMatrix.space_id == space_id,
                GedMatchMatrix.name == "Вторая линия",
            )
        )
    finally:
        db.close()
    assert after == before
    shown = client.get(f"/s/{space_id}/gedmatch?matrix={matrix_id}&show=1")
    start = shown.text.find('class="table-wrap gedmatch-matrix"')
    section = shown.text[start:shown.text.find("</table>", start)]
    assert "A1111111" in section
    assert "BX7602122" not in section
    clusters = client.get(f"/s/{space_id}/gedmatch/clusters?matrix={matrix_id}")
    assert clusters.status_code == 200
    assert "A1111111" in clusters.text
    assert "BX7602122" not in clusters.text
    assert "hidden@example.com" not in clusters.text


def _clique(kits: list[str], gen: float, cm: float, directed: dict, generations: dict) -> None:
    for index, kit in enumerate(kits):
        for other in kits[index + 1 :]:
            directed[(kit, other)] = cm
            generations[(kit, other)] = gen


def test_split_loose_clusters_breaks_a_weak_bridge_at_ten():
    left = [f"A{index}" for index in range(5)]
    right = [f"B{index}" for index in range(5)]
    directed: dict[tuple[str, str], float | None] = {}
    generations: dict[tuple[str, str], float | None] = {}
    _clique(left, 2.0, 200, directed, generations)
    _clique(right, 2.0, 180, directed, generations)
    directed[(left[0], right[0])] = 50
    generations[(left[0], right[0])] = 4.5
    ten = left + right
    assert len(split_loose_clusters([ten], directed, generations)) > 1
    assert split_loose_clusters([ten], directed, generations) == [left, right]
    seven_left = [f"C{index}" for index in range(4)]
    seven_right = [f"D{index}" for index in range(3)]
    small: dict[tuple[str, str], float | None] = {}
    small_gen: dict[tuple[str, str], float | None] = {}
    _clique(seven_left, 2.0, 200, small, small_gen)
    _clique(seven_right, 2.0, 180, small, small_gen)
    small[(seven_left[0], seven_right[0])] = 50
    small_gen[(seven_left[0], seven_right[0])] = 4.5
    seven = seven_left + seven_right
    assert split_loose_clusters([seven], small, small_gen) == [seven]
    wide_left = [f"E{index}" for index in range(6)]
    wide_right = [f"F{index}" for index in range(6)]
    wide: dict[tuple[str, str], float | None] = {}
    wide_gen: dict[tuple[str, str], float | None] = {}
    _clique(wide_left, 2.0, 200, wide, wide_gen)
    _clique(wide_right, 2.0, 180, wide, wide_gen)
    wide[(wide_left[0], wide_right[0])] = 80
    wide_gen[(wide_left[0], wide_right[0])] = 2.5
    assert split_loose_clusters([wide_left + wide_right], wide, wide_gen) == [wide_left, wide_right]
    tight = [f"G{index}" for index in range(12)]
    tight_cm: dict[tuple[str, str], float | None] = {}
    tight_gen: dict[tuple[str, str], float | None] = {}
    _clique(tight, 2.0, 300, tight_cm, tight_gen)
    assert split_loose_clusters([tight], tight_cm, tight_gen) == [tight]


def test_named_matrix_assignment_keeps_hits_off_default(client):
    login(client)
    created = client.post("/admin/spaces", data={"name": "Назначение", "add_self": "on"}, follow_redirects=True)
    space_id = find_space_id(created.text, "Назначение")
    create_tables(client.app.state.engine)
    client.post(
        f"/s/{space_id}/gedmatch",
        data={"kit": "BX7602122"},
        files={"file": ("one.html", HTML.encode(), "text/html")},
        follow_redirects=True,
    )
    db = client.app.state.session_factory()
    try:
        before = db.scalar(select(func.count()).select_from(GedMatchHit).where(GedMatchHit.space_id == space_id))
        assign_matrix_kit(db, space_id, "Ksenia Sedelnik", "BX7602122")
        assign_matrix_kit(db, space_id, "Ksenia Sedelnik", "BX7602122")
        after = db.scalar(select(func.count()).select_from(GedMatchHit).where(GedMatchHit.space_id == space_id))
        matrices = {item.name: item for item in list_matrices(db, space_id)}
        memberships = db.scalar(
            select(func.count())
            .select_from(GedMatchMatrixKit)
            .where(GedMatchMatrixKit.kit == "BX7602122", GedMatchMatrixKit.matrix_id == matrices["Ksenia Sedelnik"].id)
        )
        on_named = "BX7602122" in kits_for_matrix(db, matrices["Ksenia Sedelnik"])
        on_default = "BX7602122" in kits_for_matrix(db, matrices["Ruslan Aharodnik"])
    finally:
        db.close()
    assert after == before
    assert memberships == 1
    assert on_named
    assert not on_default


def test_cluster_tree_places_branches_apart_and_skips_unlinked():
    from app.services.cluster_tree import GAP
    from app.services.gedmatch_cluster_tree import COL_W, agreed_links, layout_gedmatch_clusters

    assert agreed_links([("LINKED", 6), ("LINKED", 6)]) == {"LINKED": 6}
    assert agreed_links([("SPLIT", 6), ("SPLIT", 2)]) == {}

    people = [
        {"id": 1, "name": "Отец", "sex": "M"},
        {"id": 2, "name": "Мать", "sex": "F"},
        {"id": 3, "name": "Сын", "sex": "M"},
        {"id": 4, "name": "Дед", "sex": "M"},
        {"id": 5, "name": "Бабка", "sex": "F"},
        {"id": 6, "name": "Дядя", "sex": "M"},
        {"id": 7, "name": "Тётя", "sex": "F"},
    ]
    families = [
        {"husband_id": 4, "wife_id": 5, "child_ids": [1, 6, 7]},
        {"husband_id": 1, "wife_id": 2, "child_ids": [3]},
    ]
    scheme = layout_gedmatch_clusters(
        [
            {"label": "1", "size": 1, "people": [6]},
            {"label": "2", "size": 1, "people": [2]},
            {"label": "3", "size": 1, "people": []},
            {"label": "4", "size": 1, "people": [7]},
        ],
        people,
        families,
        focus_id=3,
    )
    uncle = _cluster_node(scheme, "1")
    mother = _cluster_node(scheme, "2")
    loose = _cluster_node(scheme, "3")
    aunt = _cluster_node(scheme, "4")
    assert uncle["x"] != mother["x"]
    assert max(uncle["x"], aunt["x"]) < mother["x"]
    assert abs(uncle["x"] - aunt["x"]) == COL_W + GAP
    assert loose["x"] != uncle["x"]
    assert loose["x"] != mother["x"]
    ancestor = next(
        node
        for node in scheme["nodes"]
        if node["kind"] == "branch" and any(line["full"] == "Дед" for line in node["lines"])
    )
    assert ancestor["y"] < uncle["y"]
    assert ancestor["x"] <= min(uncle["x"], aunt["x"])
    assert ancestor["x"] + ancestor["w"] >= max(uncle["x"] + uncle["w"], aunt["x"] + aunt["w"])
    assert _path_hits(uncle, scheme["paths"])
    assert _path_hits(mother, scheme["paths"])
    assert not _path_hits(loose, scheme["paths"])
    loose_x = f"{loose['x'] + loose['w'] / 2:g}"
    assert all(loose_x not in path["d"].split() for path in scheme["paths"])
    assert any(line["full"] == "Без дерева" for node in scheme["nodes"] for line in node["lines"])


def _cluster_node(scheme: dict, label: str) -> dict:
    title = f"Кластер {label}"
    for node in scheme["nodes"]:
        if node["kind"] == "cluster" and any(line["full"] == title for line in node["lines"]):
            return node
    raise AssertionError(title)


def _path_hits(node: dict, paths: list[dict]) -> bool:
    center = f"{node['x'] + node['w'] / 2:g}"
    top = f"{node['y']:g}"
    return any(center in path["d"].split() and top in path["d"].split() for path in paths)


def _set_email(client, username: str, email: str) -> None:
    db = client.app.state.session_factory()
    account = db.scalar(select(User).where(User.username == username))
    account.email = email
    db.commit()
    db.close()


def _add_hit(
    client,
    space_id: int,
    source: str,
    match: str,
    cm: float,
    gen: float | None = None,
    name: str = "",
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


def test_parent_child_generation_distance_is_one():
    clear = forecast_pair({("AAAA1111", "CHLD1111"): (3600.0, 1.0)}, "AAAA1111", "CHLD1111")
    assert clear["generation_distance"] == 1
    assert clear["labels"] == ["родитель или ребёнок"]
    assert clear["generation_by_label"]["родитель или ребёнок"] == 1
    assert "Расстояние в поколениях: 1." in clear["summary"]

    overlap = forecast_pair({("AAAA1111", "CHLD1111"): (3000.0, None)}, "AAAA1111", "CHLD1111")
    assert overlap["generation_distance"] is None
    assert overlap["generation_by_label"]["родитель или ребёнок"] == 1
    assert overlap["generation_by_label"]["брат или сестра"] == 0
    assert "родитель или ребёнок" in overlap["labels"]
    assert "брат или сестра" in overlap["labels"]


def test_two_parents_do_not_gain_an_edge():
    measures = {
        ("MOTHER01", "CHLD1111"): (3400.0, 1.0),
        ("FATHER01", "CHLD1111"): (3300.0, 1.0),
        ("FATHER01", "MOTHER01"): (12.0, 5.0),
    }
    mother = forecast_pair(measures, "MOTHER01", "CHLD1111")
    assert mother["generation_distance"] == 1
    assert mother["role"] == "parent"
    assert mother["child"] == "CHLD1111"
    assert mother["labels"] == ["родитель или ребёнок"]
    assert "брат или сестра" not in mother["labels"]
    assert "двое родителей" in mother["summary"]

    adults = forecast_pair(measures, "MOTHER01", "FATHER01")
    assert adults["kinship_edge"] is False
    assert adults["role"] == "two_parents"
    assert adults["child"] == "CHLD1111"
    assert adults["parents"] == ("FATHER01", "MOTHER01")
    assert adults["labels"] == []
    assert adults["generation_distance"] is None
    assert "двое родителей" in adults["summary"]
    assert "родственная связь не выводится" in adults["summary"]
    assert "брат или сестра" not in adults["summary"]

    missing = {
        ("MOTHER01", "CHLD1111"): (3400.0, None),
        ("FATHER01", "CHLD1111"): (3400.0, None),
    }
    absent = forecast_pair(missing, "MOTHER01", "FATHER01")
    assert absent["kinship_edge"] is False
    assert absent["role"] == "two_parents"
    assert absent["child"] == "CHLD1111"

    siblings = {
        ("MOTHER01", "CHLD1111"): (3400.0, None),
        ("FATHER01", "CHLD1111"): (3400.0, None),
        ("FATHER01", "MOTHER01"): (2600.0, None),
    }
    still_open = forecast_pair(siblings, "MOTHER01", "CHLD1111")
    assert "родитель или ребёнок" in still_open["labels"]
    assert "брат или сестра" in still_open["labels"]
    assert forecast_pair(siblings, "MOTHER01", "FATHER01")["role"] != "two_parents"


def test_manual_link_overrides_cm_hypothesis():
    forecast = forecast_pair({("AAAA1111", "BBBB2222"): (3000.0, None)}, "AAAA1111", "BBBB2222")
    merged = apply_manual(forecast, "mo_d", "AAAA1111", "AAAA1111", "BBBB2222")
    assert merged["conclusion_relation"] == "mo_d"
    assert merged["generation_distance"] == 1
    assert "AAAA1111 — мать, BBBB2222 — дочь" in merged["saved_sentence"]
    named = apply_manual(
        forecast,
        "mo_d",
        "AAAA1111",
        "AAAA1111",
        "BBBB2222",
        {"AAAA1111": "Мария", "BBBB2222": "   "},
    )
    assert "AAAA1111 (Мария) — мать, BBBB2222 — дочь" in named["saved_sentence"]
    assert "()" not in named["saved_sentence"]
    labels = [
        item["label"]
        for item in kinship_form_options("AAAA1111", "BBBB2222", {"AAAA1111": "Мария"})
    ]
    assert "AAAA1111 (Мария) — мать, BBBB2222 — дочь" in labels
    assert "BBBB2222 — мать, AAAA1111 (Мария) — сын" in labels
    assert "AAAA1111 (Мария) и BBBB2222 — не родственники" in labels
    bare = [item["label"] for item in kinship_form_options("AAAA1111", "BBBB2222")]
    assert "AAAA1111 — мать, BBBB2222 — дочь" in bare
    assert "()" not in "".join(bare)
    assert merged["hypothesis"]["generation_distance"] is None
    assert "брат или сестра" in merged["hypothesis"]["labels"]
    assert "родитель или ребёнок" in merged["hypothesis"]["labels"]
    assert merged["hypothesis"]["summary"] == forecast["summary"]
    grand = forecast_pair({("GMOTHER1", "GCHILD01"): (1700.0, None)}, "GMOTHER1", "GCHILD01")
    saved = apply_manual(grand, "gp_mm_f", "GMOTHER1", "GMOTHER1", "GCHILD01")
    assert saved["generation_distance"] == 2
    assert "GMOTHER1 — бабушка (мать матери), GCHILD01 — внучка" in saved["saved_sentence"]
    assert "мать матери" not in saved["hypothesis"]["summary"]


def test_only_kinship_editor_can_change_links(client):
    assert KINSHIP_EDITOR_EMAIL == "aharodnik@gmail.com"
    login(client)
    created = client.post("/admin/users", data={"username": "anna", "password": "password123"}, follow_redirects=True)
    anna_id = find_user_id(created.text, "anna")
    admin_id = find_user_id(created.text, "admin")
    opened = client.post("/admin/spaces", data={"name": "Родство", "add_self": "on"}, follow_redirects=True)
    space_id = find_space_id(opened.text, "Родство")
    client.post(
        f"/admin/spaces/{space_id}/members",
        content=f"user_id={admin_id}&user_id={anna_id}".encode(),
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    _add_hit(client, space_id, "MOTHER01", "CHLD1111", 3400, 1, name="Анна")
    _add_owner(client, space_id, "MOTHER01", "Мария", "hidden@example.com")
    _add_hit(client, space_id, "FATHER01", "CHLD1111", 3300, 1)
    _add_hit(client, space_id, "MOTHER01", "FATHER01", 12, 5)
    child_url = f"/s/{space_id}/gedmatch/clusters/MOTHER01/CHLD1111"
    adults_url = f"/s/{space_id}/gedmatch/clusters/MOTHER01/FATHER01"
    form = {"source_kit": "MOTHER01", "match_kit": "CHLD1111", "relation": "mo_d:L"}

    denied = client.get(child_url)
    assert denied.status_code == 200
    assert "Сохранить связь" not in denied.text
    assert 'action="/s/' + str(space_id) + '/gedmatch/kinship"' not in denied.text
    assert "Расстояние в поколениях: 1." in denied.text
    assert "двое родителей" in denied.text
    assert "MOTHER01 (Мария) — мать, CHLD1111 (Анна) — дочь" in denied.text
    assert "CHLD1111 (Анна) — мать" not in denied.text
    assert KINSHIP_EDITOR_EMAIL not in denied.text
    assert " кит" not in denied.text
    assert "китов" not in denied.text
    blocked = client.post(f"/s/{space_id}/gedmatch/kinship", data=form, follow_redirects=False)
    assert blocked.status_code == 403
    db = client.app.state.session_factory()
    assert db.scalar(select(func.count()).select_from(GedMatchKinship)) == 0
    db.close()

    clusters = client.get(f"/s/{space_id}/gedmatch/clusters")
    assert "Набор CHLD1111 может быть ребёнком наборов FATHER01 и MOTHER01." in clusters.text
    assert "родственная связь не выводится" in clusters.text
    adults = client.get(adults_url)
    assert adults.status_code == 200
    assert "двое родителей набора CHLD1111" in adults.text
    assert "родственная связь не выводится" in adults.text
    assert "CHLD1111 (Анна) — дочь" in adults.text
    assert "MOTHER01 (Мария) — мать" in adults.text
    assert "FATHER01 — отец" not in adults.text
    assert "Сохранить связь" not in adults.text

    _set_email(client, "admin", KINSHIP_EDITOR_EMAIL)
    editor = client.get(child_url)
    assert "Сохранить связь" in editor.text
    mother = "MOTHER01 (Мария)"
    child = "CHLD1111 (Анна)"
    assert f"{mother} — мать, {child} — дочь" in editor.text
    assert f"{child} — мать, {mother} — сын" in editor.text
    assert "()" not in editor.text
    assert KINSHIP_EDITOR_EMAIL not in editor.text
    assert "hidden@example.com" not in editor.text
    saved = client.post(f"/s/{space_id}/gedmatch/kinship", data=form, follow_redirects=True)
    assert "Связь сохранена." in saved.text
    assert f"Сохранённая связь: {mother} — мать, {child} — дочь. Расстояние в поколениях: 1." in saved.text
    assert "Гипотеза по cM:" in saved.text
    assert "Расстояние в поколениях: 1." in saved.text
    listed = client.get(f"/s/{space_id}/gedmatch")
    assert "Родственные связи" in listed.text
    assert f"{mother} — мать, {child} — дочь" in listed.text
    assert "hidden@example.com" not in listed.text

    updated = client.post(
        f"/s/{space_id}/gedmatch/kinship",
        data={"source_kit": "MOTHER01", "match_kit": "CHLD1111", "relation": "unrelated"},
        follow_redirects=True,
    )
    assert f"Сохранённая связь: {child} и {mother} — не родственники." in updated.text
    assert "Гипотеза по cM:" in updated.text
    assert "Расстояние в поколениях: 1." in updated.text
    db = client.app.state.session_factory()
    rows = list(db.scalars(select(GedMatchKinship).where(GedMatchKinship.space_id == space_id)))
    assert len(rows) == 1
    assert rows[0].relation == "unrelated"
    assert rows[0].parent_kit == ""
    db.close()

    client.post("/logout", follow_redirects=False)
    login(client, "anna", "password123")
    viewer = client.get(child_url)
    assert viewer.status_code == 200
    assert "Сохранить связь" not in viewer.text
    assert 'name="relation"' not in viewer.text
    assert f"Сохранённая связь: {child} и {mother} — не родственники." in viewer.text
    assert "Гипотеза по cM:" in viewer.text
    assert KINSHIP_EDITOR_EMAIL not in viewer.text
    again = client.post(f"/s/{space_id}/gedmatch/kinship", data=form, follow_redirects=False)
    assert again.status_code == 403
    removed = client.post(
        f"/s/{space_id}/gedmatch/kinship/delete",
        data={"source_kit": "MOTHER01", "match_kit": "CHLD1111"},
        follow_redirects=False,
    )
    assert removed.status_code == 403
    db = client.app.state.session_factory()
    assert db.scalar(select(func.count()).select_from(GedMatchKinship)) == 1
    db.close()

    client.post("/logout", follow_redirects=False)
    login(client)
    gone = client.post(
        f"/s/{space_id}/gedmatch/kinship/delete",
        data={"source_kit": "MOTHER01", "match_kit": "CHLD1111"},
        follow_redirects=True,
    )
    assert "Связь удалена." in gone.text
    assert "Сохранённая связь" not in gone.text
    db = client.app.state.session_factory()
    assert db.scalar(select(func.count()).select_from(GedMatchKinship)) == 0
    db.close()


def test_sex_from_name_uses_given_name_only():
    assert sex_from_name("*Furs Nina adm. by hvojka") == "f"
    assert sex_from_name("*Khvojnitskaya Elena") == "f"
    assert sex_from_name("Мария") == "f"
    assert sex_from_name("Анна") == "f"
    assert sex_from_name("Ольга") == "f"
    assert sex_from_name("*Durilo Dmitry adm. by hvojka") == "m"
    assert sex_from_name("") == ""
    assert sex_from_name("Alex") == ""
    assert sex_from_name("Alex Mazurkevich") == ""
    assert sex_from_name("Valery Morozov") == ""
    assert sex_from_name("Furs") == ""
    assert sex_from_name("Khvojnitskaya") == ""
    assert sex_from_name("*Sasim") == ""
    assert sex_from_name("Melnichek") == ""


def test_female_and_male_parent_child_keeps_two_hypotheses():
    forecast = forecast_pair(
        {("AAAA1111", "BBBB2222"): (3600.0, 1.0)},
        "AAAA1111",
        "BBBB2222",
        sexes={"AAAA1111": "f", "BBBB2222": "m"},
        names={"AAAA1111": "Elena", "BBBB2222": "Ivan"},
    )
    summary = forecast["summary"]
    assert "AAAA1111 (Elena) — мать, BBBB2222 (Ivan) — сын" in summary
    assert "BBBB2222 (Ivan) — отец, AAAA1111 (Elena) — дочь" in summary
    assert "AAAA1111 (Elena) — мать, BBBB2222 (Ivan) — дочь" not in summary
    assert "BBBB2222 (Ivan) — отец, AAAA1111 (Elena) — сын" not in summary
    assert "Расстояние в поколениях: 1." in summary


def test_known_females_yield_mother_and_daughter():
    plain = forecast_pair(
        {("MAMA0001", "GIRL0001"): (3600.0, 1.0)},
        "MAMA0001",
        "GIRL0001",
        sexes={"MAMA0001": "f", "GIRL0001": "f"},
    )
    assert "MAMA0001 — мать, GIRL0001 — дочь" in plain["summary"]
    assert "GIRL0001 — мать, MAMA0001 — дочь" in plain["summary"]
    assert "сын" not in plain["summary"]

    measures = {
        ("MOTHER01", "GIRL0001"): (3400.0, 1.0),
        ("FATHER01", "GIRL0001"): (3300.0, 1.0),
        ("MOTHER01", "FATHER01"): (10.0, 5.0),
    }
    sexes = {"MOTHER01": "f", "FATHER01": "m", "GIRL0001": "f"}
    mother = forecast_pair(measures, "MOTHER01", "GIRL0001", sexes)
    assert "MOTHER01 — мать, GIRL0001 — дочь" in mother["summary"]
    assert "GIRL0001 — мать" not in mother["summary"]
    assert "сын" not in mother["summary"]


def test_unknown_or_ambiguous_sex_is_not_invented():
    unknown = forecast_pair({("AAAA1111", "BBBB2222"): (3600.0, 1.0)}, "AAAA1111", "BBBB2222")
    for word in ("мать", "отец", "дочь", "сын", "бабушка", "дедушка"):
        assert word not in unknown["summary"]
    assert sex_from_name("Alex") == ""
    sexes = {
        "ALEX0001": sex_from_name("Alex Mazurkevich"),
        "NINA0001": sex_from_name("*Furs Nina adm. by hvojka"),
    }
    assert sexes["ALEX0001"] == ""
    mixed = forecast_pair(
        {("ALEX0001", "NINA0001"): (3600.0, 1.0)},
        "ALEX0001",
        "NINA0001",
        sexes=sexes,
    )
    assert "ALEX0001 — отец" not in mixed["summary"]
    assert "ALEX0001 — мать" not in mixed["summary"]
    assert "ALEX0001 — сын" not in mixed["summary"]
    assert "ALEX0001 — дочь" not in mixed["summary"]
    assert "NINA0001 — мать" in mixed["summary"] or "NINA0001 — дочь" in mixed["summary"]


def test_grandparent_line_is_not_chosen_without_x_dna():
    forecast = forecast_pair(
        {("ELENA001", "NINA0001"): (1700.0, None)},
        "ELENA001",
        "NINA0001",
        sexes={"ELENA001": "f", "NINA0001": "f"},
    )
    summary = forecast["summary"]
    for lineage in ("мать матери", "мать отца", "отец матери", "отец отца"):
        assert lineage not in summary
    assert "сёстры" in summary
    assert "ELENA001 — бабушка, NINA0001 — внучка" in summary
    assert "NINA0001 — бабушка, ELENA001 — внучка" in summary
    assert "дедушка" not in summary
    assert len(forecast["labels"]) > 1


def test_sexed_parents_of_a_child_are_not_linked():
    measures = {
        ("MOTHER01", "CHILD001"): (3400.0, 1.0),
        ("FATHER01", "CHILD001"): (3300.0, 1.0),
        ("FATHER01", "MOTHER01"): (12.0, 5.0),
    }
    girl = {"MOTHER01": "f", "FATHER01": "m", "CHILD001": "f"}
    mother = forecast_pair(measures, "MOTHER01", "CHILD001", girl)
    assert mother["kinship_edge"] is True
    assert "MOTHER01 — мать, CHILD001 — дочь" in mother["summary"]
    assert "FATHER01 — отец" in mother["summary"]
    adults = forecast_pair(measures, "MOTHER01", "FATHER01", girl)
    assert adults["kinship_edge"] is False
    assert adults["role"] == "two_parents"
    assert adults["labels"] == []
    assert "MOTHER01 — мать" in adults["summary"]
    assert "FATHER01 — отец" in adults["summary"]
    assert "CHILD001 — дочь" in adults["summary"]
    assert "родственная связь не выводится" in adults["summary"]
    boy = {"MOTHER01": "f", "FATHER01": "m", "CHILD001": "m"}
    son = forecast_pair(measures, "FATHER01", "CHILD001", boy)
    assert "FATHER01 — отец, CHILD001 — сын" in son["summary"]
    assert forecast_pair(measures, "MOTHER01", "FATHER01", boy)["kinship_edge"] is False


def test_manual_link_stays_when_sex_forecast_differs():
    forecast = forecast_pair(
        {("AAAA1111", "BBBB2222"): (3600.0, 1.0)},
        "AAAA1111",
        "BBBB2222",
        sexes={"AAAA1111": "f", "BBBB2222": "m"},
    )
    merged = apply_manual(forecast, "mo_d", "AAAA1111", "AAAA1111", "BBBB2222")
    assert "AAAA1111 — мать, BBBB2222 — дочь" in merged["saved_sentence"]
    assert "AAAA1111 — мать, BBBB2222 — сын" in merged["hypothesis"]["summary"]
    assert merged["hypothesis"]["summary"] == forecast["summary"]


def test_gen_tier_class_steps_by_generation():
    assert gen_tier_class(0) == "gedmatch-gen-1"
    assert gen_tier_class(1.09) == "gedmatch-gen-1"
    assert gen_tier_class(1.1) == "gedmatch-gen-2"
    assert gen_tier_class(2.09) == "gedmatch-gen-2"
    assert gen_tier_class(2.1) == "gedmatch-gen-3"
    assert gen_tier_class(3.2) == "gedmatch-gen-4"
    assert gen_tier_class(8.09) == "gedmatch-gen-8"
    assert gen_tier_class(8.1) == "gedmatch-gen-9"
    assert gen_tier_class(12.4) == "gedmatch-gen-9"
    assert gen_tier_class(None) is None
