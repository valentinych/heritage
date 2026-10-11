import csv
import io
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings
from app.importers.geneteka import COLUMNS
from app.main import create_app
from app.models import LinkBuild, LinkRow, Person, PersonLink, Tree
from app.services.link_trees import Record, _person_parents, cluster_records, resolve_parents, split_parents
from tests.test_heritage import find_space_id, location_path, login

GEDCOM = """\
0 HEAD
1 CHAR UTF-8
0 @I1@ INDI
1 NAME Иван /Иванов/
1 SEX M
1 BIRT
2 DATE 1800
0 TRLR
""".encode()

GEDCOM_NAMESAKES = """\
0 HEAD
1 CHAR UTF-8
0 @I1@ INDI
1 NAME Иван /Иванов/
1 SEX M
1 BIRT
2 DATE 1800
0 @I2@ INDI
1 NAME Иван /Иванов/
1 SEX M
1 BIRT
2 DATE 1840
0 TRLR
""".encode()


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


def rec(kind, year, parish="Полонка", given="Иван", surname="Иванов", spouse_given="", spouse_surname=""):
    return Record(
        dataset_id=1,
        slot=0,
        kind=kind,
        year=year,
        given=given,
        surname=surname,
        parish=parish,
        spouse_given=spouse_given,
        spouse_surname=spouse_surname,
    )


def test_same_parish_birth_marriage_and_death_are_one_person():
    result = cluster_records(
        [
            rec("birth", 1800),
            rec("marriage", 1820, spouse_given="Мария", spouse_surname="Петрова"),
            rec("death", 1860),
        ]
    )
    assert len(result.identities) == 1
    person = result.identities[0]
    assert person.birth_year == 1800
    assert len(person.marriages) == 1
    assert person.death is not None
    assert person.death.year == 1860
    assert result.cross == 0


def test_later_birth_of_the_same_name_is_not_merged():
    result = cluster_records(
        [
            rec("birth", 1800),
            rec("birth", 1840),
            rec("marriage", 1820),
            rec("death", 1860),
        ]
    )
    assert len(result.identities) == 2
    early, late = sorted(result.identities, key=lambda item: item.birth_year)
    assert len(early.marriages) == 1
    assert late.marriages == []
    assert early.death is None
    assert late.death is None
    assert result.ambiguous == 1


def test_marriage_outside_16_to_35_does_not_attach():
    def marriages(year):
        result = cluster_records([rec("birth", 1800), rec("marriage", year)])
        return result.identities[0].marriages

    assert marriages(1810) == []
    assert len(marriages(1816)) == 1
    assert len(marriages(1835)) == 1
    assert marriages(1850) == []


def test_death_before_birth_or_past_100_years_does_not_attach():
    before = cluster_records([rec("birth", 1800), rec("death", 1799)])
    assert before.identities[0].death is None
    too_long = cluster_records([rec("birth", 1800), rec("death", 1901)])
    assert too_long.identities[0].death is None
    fits = cluster_records([rec("birth", 1800), rec("marriage", 1820), rec("death", 1900)])
    assert fits.identities[0].death is not None
    before_marriage = cluster_records([rec("birth", 1800), rec("marriage", 1820), rec("death", 1820)])
    assert len(before_marriage.identities[0].marriages) == 1
    assert before_marriage.identities[0].death is None


def test_cross_parish_only_when_same_parish_has_no_candidate():
    preferred = cluster_records(
        [
            rec("birth", 1800, parish="Полонка"),
            rec("birth", 1800, parish="Ивье"),
            rec("marriage", 1820, parish="Полонка"),
        ]
    )
    by_parish = {item.parish: item for item in preferred.identities}
    assert len(by_parish["Полонка"].marriages) == 1
    assert by_parish["Ивье"].marriages == []
    assert preferred.cross == 0

    crossed = cluster_records(
        [
            rec("birth", 1810, parish="Полонка"),
            rec("birth", 1800, parish="Ивье"),
            rec("marriage", 1820, parish="Полонка"),
        ]
    )
    by_parish = {item.parish: item for item in crossed.identities}
    assert by_parish["Полонка"].marriages == []
    assert len(by_parish["Ивье"].marriages) == 1
    assert crossed.cross == 1


def _sheet(rows: list[dict[str, str]]) -> bytes:
    labels = {"birth": "рождение", "marriage": "брак", "death": "смерть"}
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(COLUMNS), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        full = {column: "" for column in COLUMNS}
        full.update(row)
        full["Событие"] = labels.get(full["Событие"], full["Событие"])
        if not full["Фио"]:
            full["Фио"] = " ".join(part for part in (full["Имя"], full["Фамилия"]) if part)
        if full["Событие"] == "рождение" and not full["Год рождения"]:
            full["Год рождения"] = full["Год"]
        writer.writerow(full)
    return buffer.getvalue().encode("utf-8")


def _event(kind, year, parish="Полонка", given="Иван", surname="Иванов"):
    return {
        "Событие": kind,
        "Год": str(year),
        "Имя": given,
        "Фамилия": surname,
        "Приход": parish,
        "Имя супруга": "Мария" if kind == "marriage" else "",
        "Фамилия супруга": "Петрова" if kind == "marriage" else "",
    }


def _space(client: TestClient, name: str) -> int:
    login(client)
    created = client.post("/admin/spaces", data={"name": name, "add_self": "on"}, follow_redirects=True)
    return find_space_id(created.text, name)


def _tree(client: TestClient, space_id: int, content: bytes, filename: str = "tree.ged"):
    response = client.post(
        f"/s/{space_id}/trees",
        files={"file": (filename, content, "application/octet-stream")},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def _links(client: TestClient, space_id: int, rows: list[dict[str, str]]):
    uploaded = client.post(
        f"/s/{space_id}/links",
        files={"file": ("parish.csv", _sheet(rows), "text/csv")},
        follow_redirects=False,
    )
    assert uploaded.status_code == 303, uploaded.text
    saved = client.post(
        location_path(uploaded),
        data={"name": "Фио", "birth_year": "Год рождения"},
        follow_redirects=True,
    )
    assert saved.status_code == 200, saved.text
    return saved


def _snapshot(client: TestClient, space_id: int):
    db = client.app.state.session_factory()
    try:
        rows = [
            {"birth_year": row.birth_year, "person_id": row.person_id, "name": row.name}
            for row in db.scalars(select(LinkRow).where(LinkRow.space_id == space_id).order_by(LinkRow.id))
        ]
        people = {
            person.birth_year: person.id
            for person in db.scalars(select(Person).join(Tree, Tree.id == Person.tree_id).where(Tree.space_id == space_id))
        }
        links = db.scalar(select(func.count()).select_from(PersonLink).where(PersonLink.space_id == space_id)) or 0
        return rows, people, links
    finally:
        db.close()


def test_button_links_same_parish_person_and_second_run_does_not_duplicate(client):
    space_id = _space(client, "Деревья")
    _tree(client, space_id, GEDCOM)
    page = _links(
        client,
        space_id,
        [_event("birth", 1800), _event("marriage", 1820), _event("death", 1860)],
    )
    assert "Собрать деревья" in page.text
    assert "брак в 16–35 лет" in page.text

    first = client.post(f"/s/{space_id}/links/trees", follow_redirects=True)
    assert first.status_code == 200, first.text
    assert "К дереву привязано: 1. Неоднозначно: 0. Между приходами: 0." in first.text
    rows, people, links = _snapshot(client, space_id)
    assert [row["birth_year"] for row in rows] == [1800, None, None]
    assert rows[0]["person_id"] == people[1800]
    assert rows[1]["person_id"] is None
    assert rows[2]["person_id"] is None
    assert links == 0

    second = client.post(f"/s/{space_id}/links/trees", follow_redirects=True)
    assert second.status_code == 200, second.text
    again, _people, again_links = _snapshot(client, space_id)
    assert again == rows
    assert again_links == 0


def test_namesake_birth_is_a_different_tree_person(client):
    space_id = _space(client, "Однофамильцы")
    _tree(client, space_id, GEDCOM_NAMESAKES)
    _links(
        client,
        space_id,
        [_event("birth", 1800), _event("birth", 1840), _event("marriage", 1820), _event("death", 1860)],
    )
    response = client.post(f"/s/{space_id}/links/trees", follow_redirects=True)
    assert response.status_code == 200, response.text
    rows, people, _links_count = _snapshot(client, space_id)
    births = {row["birth_year"]: row["person_id"] for row in rows if row["birth_year"]}
    assert births[1800] == people[1800]
    assert births[1840] == people[1840]
    assert births[1800] != births[1840]


def test_cross_parish_link_is_saved_when_parish_has_no_candidate(client):
    space_id = _space(client, "Приходы")
    _tree(client, space_id, GEDCOM)
    _links(
        client,
        space_id,
        [
            _event("birth", 1800, parish="Ивье"),
            _event("marriage", 1820, parish="Полонка"),
            _event("death", 1860, parish="Ивье"),
        ],
    )
    response = client.post(f"/s/{space_id}/links/trees", follow_redirects=True)
    assert response.status_code == 200, response.text
    assert "К дереву привязано: 1. Неоднозначно: 0. Между приходами: 1." in response.text
    rows, people, _links_count = _snapshot(client, space_id)
    assert rows[0]["person_id"] == people[1800]


def _assembled(html: str) -> str:
    marker = 'aria-label="Собранные люди"'
    start = html.find(marker)
    assert start != -1, html
    end = html.find("</table>", start)
    assert end != -1, html
    return html[start:end]


def _body_rows(fragment: str) -> list[str]:
    body = fragment.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    return [chunk for chunk in body.split("<tr>") if chunk.strip()]


def _stored_people(client: TestClient, space_id: int) -> list[dict]:
    db = client.app.state.session_factory()
    try:
        build = db.get(LinkBuild, space_id)
        assert build is not None
        return json.loads(build.people)
    finally:
        db.close()


def test_assembled_table_lists_cross_parish_and_ambiguous_namesakes(client):
    space_id = _space(client, "Список")
    _tree(client, space_id, GEDCOM)
    before = _links(
        client,
        space_id,
        [
            _event("birth", 1800, parish="Ивье"),
            _event("marriage", 1820, parish="Полонка"),
            _event("death", 1860, parish="Ивье"),
            _event("birth", 1810, parish="Полонка", given="Пётр", surname="Петров"),
            _event("birth", 1812, parish="Полонка", given="Пётр", surname="Петров"),
            _event("marriage", 1830, parish="Полонка", given="Пётр", surname="Петров"),
            _event("birth", 1790, parish="Полонка", given="Сидор", surname="Сидоров"),
        ],
    )
    assert 'name="q"' in before.text
    assert "Список людей появится после сборки деревьев." in before.text
    assert 'aria-label="Собранные люди"' not in before.text

    first = client.post(f"/s/{space_id}/links/trees", follow_redirects=True)
    assert first.status_code == 200, first.text
    assert "К дереву привязано: 1. Неоднозначно: 1. Между приходами: 1." in first.text
    rows = _body_rows(_assembled(first.text))
    assert len(rows) == 4
    assert [chunk for chunk in rows if "Иванов" in chunk and "Петров" not in chunk]
    ivan = next(chunk for chunk in rows if "Ивье · Полонка" in chunk)
    assert "Иван Иванов" in ivan
    assert f"/s/{space_id}/people/" in ivan
    assert "1820" in ivan
    assert "1860" in ivan
    namesakes = [chunk for chunk in rows if "Петров" in chunk]
    assert len(namesakes) == 2
    assert {1810, 1812} == {1810 if "1810" in chunk else 1812 for chunk in namesakes}
    for chunk in namesakes:
        assert "неоднозначно" in chunk
        assert "нет" in chunk
        assert "/people/" not in chunk
    assert any("Сидоров" in chunk for chunk in rows)
    assert len(_stored_people(client, space_id)) == 4

    reloaded = client.get(f"/s/{space_id}/links")
    assert len(_body_rows(_assembled(reloaded.text))) == 4

    hidden = client.get(f"/s/{space_id}/links", params={"q": "Сидоров"})
    hidden_table = _assembled(hidden.text)
    assert "Показано: 1." in hidden.text
    assert "Сидоров" in hidden_table
    assert "Иванов" not in hidden_table
    assert "Петров" not in hidden_table

    folded = client.get(f"/s/{space_id}/links", params={"q": "Пётров"})
    folded_table = _assembled(folded.text)
    assert "Показано: 2." in folded.text
    assert folded_table.count("неоднозначно") == 2
    assert "Иванов" not in folded_table
    assert "Сидоров" not in folded_table

    second = client.post(f"/s/{space_id}/links/trees", follow_redirects=True)
    assert len(_body_rows(_assembled(second.text))) == 4
    assert len(_stored_people(client, space_id)) == 4


def test_assembled_table_shows_birth_parents_and_marriage_spouse_parents(client):
    space_id = _space(client, "Родители")
    birth = _event("birth", 1800)
    birth.update({"Имя отца": "Ян", "Имя матери": "Анна", "Фамилия матери": "Новак"})
    marriage = _event("marriage", 1820)
    marriage.update({"Родители": "отец Пётр", "Родители супруга": "мать Ева"})
    orphan = _event("birth", 1810, given="Сидор", surname="Сидоров")
    death = _event("death", 1880, given="Сидор", surname="Сидоров")
    death.update({"Имя отца": "Кузьма", "Имя матери": "Ольга", "Фамилия матери": "Лес"})
    bare = _event("birth", 1790, given="Пётр", surname="Петров")
    bare_marriage = _event("marriage", 1810, given="Пётр", surname="Петров")
    bare_marriage.update({"Родители": "отец Пётр", "Родители супруга": "мать Анна"})
    _links(client, space_id, [birth, marriage, orphan, death, bare, bare_marriage])
    page = client.post(f"/s/{space_id}/links/trees", follow_redirects=True)
    assert page.status_code == 200, page.text
    table = _assembled(page.text)
    assert "Отец" in table
    assert "Мать" in table
    assert "Фамилия матери" in table
    assert "Родители супруга" in table
    ivan = next(chunk for chunk in _body_rows(table) if "Иванов" in chunk)
    assert "Ян" in ivan
    assert "Анна" in ivan
    assert "Новак" in ivan
    assert "мать Ева" in ivan
    assert "отец Пётр" not in ivan
    sidor = next(chunk for chunk in _body_rows(table) if "Сидоров" in chunk)
    assert "Кузьма" in sidor
    assert "Ольга" in sidor
    assert "Лес" in sidor
    petr = next(chunk for chunk in _body_rows(table) if "Петров" in chunk)
    assert "отец Пётр" in petr
    assert "мать Анна" in petr


def _birth(year, given, surname, parish="Nowa Mysz", act="", father="", mother="", mother_surname=""):
    return Record(
        dataset_id=1,
        slot=0,
        kind="birth",
        year=year,
        given=given,
        surname=surname,
        parish=parish,
        parish_label=parish,
        act=act,
        father=father,
        mother=mother,
        mother_surname=mother_surname,
    )


def _helena(*others: Record):
    child = _birth(
        1798,
        "Helena",
        "Paszkiewicz",
        act="12",
        father="Jakub",
        mother="Urszula",
        mother_surname="Ciecierszczanka",
    )
    result = cluster_records([child, *others])
    resolve_parents(result.identities)
    return next(item for item in result.identities if item.given == "Helena")


def test_parent_string_splits_only_two_geneteka_shapes():
    assert split_parents("Piotr, Katarzyna Nowicka") == ("Piotr", "", "Katarzyna", "Nowicka")
    assert split_parents("Jan Kowalski, Anna Nowak") == ("Jan", "Kowalski", "Anna", "Nowak")
    assert split_parents("отец Пётр") is None
    assert split_parents("Piotr, Anna") is None


def test_helena_father_matches_one_earlier_birth_in_the_same_parish():
    helena = _helena(_birth(1773, "Jakub", "Paszkiewicz", act="4"))
    assert helena.father_match == "1773, акт 4, Nowa Mysz"
    facts = helena.birth
    assert facts.father == "Jakub"


def test_helena_father_is_missing_when_the_only_jakub_is_too_young():
    helena = _helena(_birth(1788, "Jakub", "Paszkiewicz", act="4"))
    assert helena.father_match == ""


def test_helena_father_is_ambiguous_when_two_jakubs_fit():
    helena = _helena(
        _birth(1770, "Jakub", "Paszkiewicz", act="1"),
        _birth(1773, "Jakub", "Paszkiewicz", act="4"),
    )
    assert helena.father_match == "неоднозначно"


def test_mother_matches_maiden_surname_not_the_child_surname():
    helena = _helena(
        _birth(1770, "Urszula", "Ciecierszczanka", act="7"),
        _birth(1771, "Urszula", "Paszkiewicz", act="8"),
    )
    assert helena.mother_match == "1770, акт 7, Nowa Mysz"


def test_clear_marriage_parent_string_fills_names_and_uses_child_surname():
    child = _birth(1820, "Jan", "Keller")
    marriage = rec("marriage", 1840, parish="Nowa Mysz", given="Jan", surname="Keller")
    marriage.parents = "Piotr, Katarzyna Nowicka"
    father = _birth(1795, "Piotr", "Keller", act="3")
    result = cluster_records([child, marriage, father])
    resolve_parents(result.identities)
    jan = next(item for item in result.identities if item.given == "Jan")
    facts = _person_parents(jan)
    assert facts["father"] == "Piotr"
    assert facts["mother"] == "Katarzyna"
    assert facts["mother_surname"] == "Nowicka"
    assert facts["parents"] == ""
    assert jan.father_match == "1795, акт 3, Nowa Mysz"


def test_explicit_father_surname_is_not_replaced_by_the_child_surname():
    child = _birth(1820, "Jan", "Keller")
    marriage = rec("marriage", 1840, parish="Nowa Mysz", given="Jan", surname="Keller")
    marriage.parents = "Piotr Nowicki, Anna Zima"
    same = _birth(1795, "Piotr", "Keller", act="3")
    own = _birth(1794, "Piotr", "Nowicki", act="9")
    result = cluster_records([child, marriage, same, own])
    resolve_parents(result.identities)
    jan = next(item for item in result.identities if item.given == "Jan")
    assert jan.father_match == "1794, акт 9, Nowa Mysz"


def test_links_table_shows_parent_year_act_and_parish(client):
    space_id = _space(client, "Индекс родителей")
    helena = _event("birth", 1798, parish="Nowa Mysz", given="Helena", surname="Paszkiewicz")
    helena.update({"Акт": "12", "Имя отца": "Jakub", "Имя матери": "Urszula", "Фамилия матери": "Ciecierszczanka"})
    jakub = _event("birth", 1773, parish="Nowa Mysz", given="Jakub", surname="Paszkiewicz")
    jakub.update({"Акт": "4"})
    urszula = _event("birth", 1770, parish="Nowa Mysz", given="Urszula", surname="Ciecierszczanka")
    urszula.update({"Акт": "7"})
    _links(client, space_id, [helena, jakub, urszula])
    page = client.post(f"/s/{space_id}/links/trees", follow_redirects=True)
    assert page.status_code == 200, page.text
    table = _assembled(page.text)
    assert "Отец в индексе" in table
    assert "Мать в индексе" in table
    row = next(chunk for chunk in _body_rows(table) if "Helena" in chunk)
    assert "Jakub" in row
    assert "Urszula" in row
    assert "Ciecierszczanka" in row
    assert "1773, акт 4, Nowa Mysz" in row
    assert "1770, акт 7, Nowa Mysz" in row
    assert len(_body_rows(table)) == 3


def test_surname_search_lists_every_stored_birth(client, monkeypatch):
    space_id = _space(client, "Поиск фамилии")
    helena = _event("birth", 1798, parish="Nowa Mysz", given="Helena", surname="Paszkiewicz")
    kazimierz = _event("birth", 1798, parish="Nowa Mysz", given="Kazimierz", surname="Paszkiewicz")
    later = _event("birth", 1810, parish="Nowa Mysz", given="Helena", surname="Paszkiewicz")
    undated = _event("birth", 1800, parish="Nowa Mysz", given="Jan", surname="Paszkiewicz")
    undated["Год"] = ""
    undated["Год рождения"] = ""
    other = _event("birth", 1801, parish="Nowa Mysz", given="Adam", surname="Nowak")
    bazyli = _event("birth", 1810, parish="Darewo", given="Bazyli", surname="Paszkiewicz")
    _links(client, space_id, [helena, kazimierz, later, undated, other])
    _links(client, space_id, [bazyli])
    saved = client.post(f"/s/{space_id}/links/trees", follow_redirects=True)
    assert saved.status_code == 200, saved.text

    page = client.get(f"/s/{space_id}/links", params={"q": "Paszkiewicz"})
    assert "Показано: 5." in page.text
    table = _assembled(page.text)
    assert "Nowak" not in table
    assert table.count("Helena") == 2
    assert "Kazimierz" in table
    assert "Jan" in table
    assert "Bazyli" in table
    assert "Darewo" in table
    assert len(_body_rows(table)) == 5

    monkeypatch.setattr("app.routes.datasets.PEOPLE_PAGE", 2)
    first = client.get(f"/s/{space_id}/links", params={"q": "Paszkiewicz"})
    assert "Показано: 1–2 из 5." in first.text
    assert "Следующая страница" in first.text
    second = client.get(f"/s/{space_id}/links", params={"q": "Paszkiewicz", "people_page": 2})
    third = client.get(f"/s/{space_id}/links", params={"q": "Paszkiewicz", "people_page": 3})
    assert "Следующая страница" in second.text
    assert "Следующая страница" not in third.text
    seen = _assembled(first.text) + _assembled(second.text) + _assembled(third.text)
    assert seen.count("Helena") == 2
    assert "Kazimierz" in seen
    assert "Jan" in seen
    assert "Bazyli" in seen
    assert "Nowak" not in seen
