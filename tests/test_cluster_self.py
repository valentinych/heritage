import re

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.test_heritage import GEDCOM, find_space_id, location_path, login


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


def _options(html: str, name: str) -> list[str]:
    block = re.search(rf'<select name="{name}".*?</select>', html, re.S)
    assert block, html
    return re.findall(r'<option value="([^"]*)"', block.group(0))


def _upload(client, space_id, filename, body, owner="", owner_other=""):
    uploaded = client.post(
        f"/s/{space_id}/clusters",
        data={"owner": owner, "owner_other": owner_other},
        files={"file": (filename, body.encode(), "text/csv")},
        follow_redirects=False,
    )
    assert uploaded.status_code == 303, uploaded.text
    page = client.post(
        location_path(uploaded),
        data={"cluster": "Кластер", "name": "Имя", "shared_cm": "cM"},
        follow_redirects=True,
    )
    assert page.status_code == 200, page.text
    return page


def test_self_scheme_joins_shared_people_and_keeps_strangers_apart(client):
    login(client)
    created = client.post("/admin/spaces", data={"name": "Кластеры", "add_self": "on"}, follow_redirects=True)
    space_id = find_space_id(created.text, "Кластеры")
    _upload(client, space_id, "anna.csv", "Кластер;Имя;cM\n1;Борис;40\n1;Вера;30\n", owner_other="Анна")
    _upload(client, space_id, "petr.csv", "Кластер;Имя;cM\n2;Вера;20\n7;Егор;15\n", owner_other="Пётр")
    _upload(client, space_id, "olga.csv", "Кластер;Имя;cM\n3;Зина;10\n", owner_other="Ольга")

    before = client.get(f"/s/{space_id}/clusters")
    assert _options(before.text, "name") == ["Анна", "Ольга", "Пётр"]
    assert "Борис" not in _options(before.text, "name")

    marked = client.post(f"/s/{space_id}/clusters/self", data={"name": "Анна"}, follow_redirects=True)
    assert marked.status_code == 200, marked.text
    assert "Сейчас вы: Анна." in marked.text
    assert "Вы, Анна" in marked.text
    self_part, other = marked.text.split("<h2>Кластеры Ольга</h2>", 1)
    assert "Кластер 7" in self_part
    assert "Егор" in self_part
    assert "Кластер 3" not in self_part
    assert "Кластер 3" in other
    assert "Зина" in other

    _upload(client, space_id, "anna-new.csv", "Кластер;Имя;cM\n1;Борис;40\n1;Вера;25\n", owner_other="Анна")
    page = client.get(f"/s/{space_id}/clusters")
    assert page.status_code == 200, page.text
    assert page.text.count("anna.csv") == 0
    assert page.text.count("anna-new.csv") == 1
    assert page.text.count("Чьи кластеры: Анна") == 1
    assert "Чьи кластеры: Пётр" in page.text
    rejected = client.post(f"/s/{space_id}/clusters/self", data={"name": "Борис"}, follow_redirects=True)
    assert "Сначала укажите этого человека" in rejected.text
    assert "Сейчас вы: Анна." in rejected.text


def test_owner_list_comes_from_clusters_not_the_tree(client):
    login(client)
    created = client.post("/admin/spaces", data={"name": "Дерево", "add_self": "on"}, follow_redirects=True)
    space_id = find_space_id(created.text, "Дерево")
    client.post(
        f"/s/{space_id}/trees",
        files={"file": ("family.ged", GEDCOM, "application/octet-stream")},
        follow_redirects=False,
    )
    page = _upload(client, space_id, "ivan.csv", "Кластер;Имя;cM\n1;Борис;40\n", owner_other="Анна")
    owners = _options(page.text, "owner")
    assert "Борис" in owners
    assert "Иван Иванов" not in owners
    assert "Мария Петрова" not in owners
    assert "Чьи кластеры: Анна" in page.text
    assert _options(page.text, "name") == ["Анна"]
