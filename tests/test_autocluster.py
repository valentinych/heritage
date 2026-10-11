import re

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.importers.autocluster import parse_autocluster
from app.main import create_app
from tests.test_heritage import find_space_id, location_path, login


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
var json = {"nodes":[
{"index":0,"cM":40.0,"url":"http://www.myheritage.com/dna/match/D-AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA-D-BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB?forcelogin=secret@example.com","segments":2,"personCount":3,"treeId":"https://example.com/tree","groupName":"3rd cousin","name":"Анна","largestCm":12.5,"icw":1,"group":1,"note":null},
{"index":1,"cM":20.5,"url":"http://www.myheritage.com/dna/match/D-AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA-D-CCCCCCCC-CCCC-CCCC-CCCC-CCCCCCCCCCCC?forcelogin=secret@example.com","segments":1,"personCount":1,"treeId":"","groupName":"distant","name":"Борис","largestCm":9.0,"icw":1,"group":1,"note":null},
{"index":2,"cM":15.0,"url":"http://www.myheritage.com/dna/match/D-AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA-D-DDDDDDDD-DDDD-DDDD-DDDD-DDDDDDDDDDDD","segments":4,"personCount":"","treeId":"https://example.com/v","groupName":"distant","name":"Вера","largestCm":8.0,"icw":0,"group":2,"note":null}
],"links":[{"source":0,"target":1,"value":250},{"source":0,"target":2,"value":0}],"clusters":[]}
var tableData = [];
"""

CSV = (
    "Identifier,Name,total shared cM,Tree,Tree Person Count,Notes,Cluster,1_Анна,2_Борис,3_Вера\n"
    "KITANNA,Анна,40,https://example.com/tree,3,,1,1,2,\n"
    "KITBORIS,Борис,20.5,,1,заметка,1,,2,\n"
    "KITVERA,Вера,15,,0,,2,,,3\n"
)


def test_parse_html_drops_email():
    parsed = parse_autocluster("clusters.html", HTML.encode())
    assert parsed is not None
    assert len(parsed.people) == 3
    assert parsed.pairs == [(0, 1)]
    assert parsed.people[0].kit == "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB"
    assert parsed.people[0].segments == 2
    assert all("@" not in person.kit and "@" not in person.tree_url for person in parsed.people)


def test_parse_csv_matrix():
    parsed = parse_autocluster("clusters.csv", CSV.encode())
    assert parsed is not None
    assert [person.cluster for person in parsed.people] == ["1", "1", "2"]
    assert parsed.pairs == [(0, 1)]
    assert parsed.people[1].note == "заметка"


def test_matrix_cell_opens_details(client: TestClient):
    login(client)
    created = client.post("/admin/spaces", data={"name": "Кластеры HTML", "add_self": "on"}, follow_redirects=True)
    space_id = find_space_id(created.text, "Кластеры HTML")
    uploaded = client.post(
        f"/s/{space_id}/clusters",
        files={"file": ("clusters.html", HTML.encode(), "text/html")},
        follow_redirects=False,
    )
    assert uploaded.status_code == 303
    matrix = client.get(location_path(uploaded))
    assert matrix.status_code == 200
    assert "secret@example.com" not in matrix.text
    assert matrix.text.count('class="ac-h1"') >= 1
    assert matrix.text.count(">Совпадение<") == 2
    assert "ac-cross" not in matrix.text
    href = re.search(r'href="(/s/\d+/clusters/\d+/pairs/\d+)"', matrix.text)
    assert href
    detail = client.get(href.group(1))
    assert detail.status_code == 200
    assert "secret@example.com" not in detail.text
    assert "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB" in detail.text
    assert "40.0" in detail.text
    assert "20.5" in detail.text
    assert ">2<" in detail.text
    assert "3rd cousin" in detail.text
    assert "https://example.com/tree" in detail.text


def test_csv_upload_builds_matrix(client: TestClient):
    login(client)
    created = client.post("/admin/spaces", data={"name": "Кластеры CSV", "add_self": "on"}, follow_redirects=True)
    space_id = find_space_id(created.text, "Кластеры CSV")
    uploaded = client.post(
        f"/s/{space_id}/clusters",
        files={"file": ("clusters.csv", CSV.encode(), "text/csv")},
        follow_redirects=False,
    )
    assert uploaded.status_code == 303
    assert "/matrix" in uploaded.headers["location"]
    page = client.get(f"/s/{space_id}/clusters")
    assert "Матрица совпадений" in page.text
