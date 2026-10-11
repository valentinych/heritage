import re
from urllib.parse import quote

from sqlalchemy import delete

from app.models import ArtifactSettlement
from app.services.vesi import (
    NO_INFO,
    build_index,
    ensure_artifact_settlements,
    get_artifact,
    has_information,
    haversine_km,
    load_index,
    load_parishes,
    load_prikhody,
    marker_for,
    marker_tone,
    place_title,
    saved_bindings,
    settlement_from_place,
)
from tests.test_heritage import client, login


def test_place_name_drops_the_district():
    assert settlement_from_place("Подлесейки, Новогрудский район") == "Подлесейки"


def test_place_title_keeps_the_village_and_drops_cemetery_words():
    assert place_title("Новосады полевое") == "Новосады"
    assert place_title("Новосады старое") == "Новосады"
    assert place_title("Старое Село") == "Старое Село"
    assert place_title("Барановичи. Старое католическое кладбище") == "Барановичи"
    assert place_title("еврейское кладбище через забор") == ""
    assert place_title("Jüdischer Friedhof") == ""
    assert place_title("Кладбище (старое) Заславль") == "Заславль"


def _artifacts(informed: int, total: int, label: str = "Свободный доступ") -> list[dict]:
    return [{"label": label}] * informed + [{"label": NO_INFO["label"]}] * (total - informed)


def test_marker_tone_thresholds():
    assert marker_tone(_artifacts(0, 1)) == "red"
    assert marker_tone(_artifacts(0, 5)) == "red"
    assert marker_tone([]) == "red"
    assert marker_for(_artifacts(0, 4)) == {
        "marker_class": "vesi-dot-red",
        "marker_label": "нет сведений",
    }
    assert marker_tone(_artifacts(1, 5)) == "yellow"
    assert marker_for(_artifacts(1, 5)) == {
        "marker_class": "vesi-dot-yellow",
        "marker_label": "мало сведений",
    }
    assert marker_tone(_artifacts(1, 4)) == "green"
    assert marker_tone(_artifacts(1, 1)) == "green"
    assert marker_for(_artifacts(1, 1)) == {
        "marker_class": "vesi-dot-green",
        "marker_label": "есть сведения",
    }
    for label in ("Доступ за донат", "Только сканы", "Информация в обработке"):
        assert marker_tone(_artifacts(1, 1, label)) == "green"
        assert marker_tone(_artifacts(1, 5, label)) == "yellow"
        assert marker_tone(_artifacts(1, 4, label)) == "green"


def test_vesi_is_public_and_lists_surnames(client):
    page = client.get("/vesi", follow_redirects=False)
    assert page.status_code == 200
    assert page.headers.get("location") is None
    assert "Города и веси" in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert "tile.openstreetmap.org" in page.headers["content-security-policy"]
    assert page.headers["referrer-policy"] == "strict-origin-when-cross-origin"

    locked = client.get("/login")
    assert "tile.openstreetmap.org" not in locked.headers["content-security-policy"]
    assert locked.headers["referrer-policy"] == "same-origin"

    payload = client.get("/vesi/data")
    assert payload.status_code == 200
    assert "tile.openstreetmap.org" not in payload.headers["content-security-policy"]
    assert payload.headers["referrer-policy"] == "same-origin"
    settlements = payload.json()["settlements"]
    assert 5600 < len(settlements) < 6877
    novosady = next(
        item
        for item in settlements
        if any(artifact["id"] == "новосады" for artifact in item["artifacts"])
    )
    novosady_cemetery = next(artifact for artifact in novosady["artifacts"] if artifact["id"] == "новосады")
    assert novosady_cemetery["label"] == "Свободный доступ"
    assert novosady_cemetery["emoji"] == "✅"
    assert novosady_cemetery["indexed"] is True
    assert novosady["marker_class"] == "vesi-dot-green"
    assert novosady["marker_label"] == "есть сведения"
    for item in settlements:
        expected = marker_for(item["artifacts"])
        assert item["marker_class"] == expected["marker_class"]
        assert item["marker_label"] == expected["marker_label"]
    red = next(
        item
        for item in settlements
        if item["artifacts"] and all(artifact["label"] == NO_INFO["label"] for artifact in item["artifacts"])
    )
    assert red["marker_class"] == "vesi-dot-red"
    css = client.get("/static/vesi.css").text
    script = client.get("/static/vesi.js").text
    for name in ("vesi-dot-red", "vesi-dot-yellow", "vesi-dot-green"):
        assert name in css
        assert name in script
    assert "нет сведений" in script
    assert "мало сведений" in script
    assert "есть сведения" in script
    assert "style=" not in script
    labels = {artifact["label"] for item in settlements for artifact in item["artifacts"]}
    assert labels == {"Свободный доступ", "Нет информации"}
    indexed = [artifact for item in settlements for artifact in item["artifacts"] if artifact["indexed"]]
    assert len(indexed) == 23
    assert all(artifact["label"] == "Свободный доступ" and artifact["emoji"] == "✅" for artifact in indexed)
    lookalike_settlement = next(
        item for item in settlements if any(artifact["name"] == "Новосады полевое" for artifact in item["artifacts"])
    )
    lookalike = next(artifact for artifact in lookalike_settlement["artifacts"] if artifact["name"] == "Новосады полевое")
    assert lookalike_settlement["name"] == "Новосады"
    assert lookalike_settlement["id"] != novosady["id"]
    assert haversine_km(lookalike_settlement["lat"], lookalike_settlement["lon"], novosady["lat"], novosady["lon"]) > 4
    assert any(artifact["name"] == "Новосады старое" for artifact in lookalike_settlement["artifacts"])
    assert lookalike["indexed"] is False
    assert lookalike["label"] == NO_INFO["label"]
    assert lookalike["emoji"] == NO_INFO["emoji"]
    vselub = next(item for item in settlements if item["name"] == "Вселюб")
    assert {artifact["name"] for artifact in vselub["artifacts"]} >= {
        "Вселюб Католическое",
        "Вселюб Православное",
    }
    lyubcha = next(item for item in settlements if item["name"] == "Любча" and len(item["artifacts"]) > 1)
    assert {artifact["name"] for artifact in lyubcha["artifacts"]} >= {"Любча 1Мая", "Любча Ленина"}

    surnames = client.get("/vesi/" + quote("новосады"))
    assert surnames.status_code == 200
    assert "Скобялко" in surnames.text
    assert "Свободный доступ" in surnames.text
    assert f'/mogilki?cemetery={quote("Новосады")}' in surnames.text
    assert ">nid<" not in surnames.text.casefold()
    assert "tile.openstreetmap.org" not in surnames.headers["content-security-policy"]
    assert surnames.headers["referrer-policy"] == "same-origin"

    yushkovichi = client.get("/vesi/" + quote("юшковичи"))
    assert "Петрушкевич" in yushkovichi.text
    for artifact_id in ("делятичи", "черешля"):
        assert get_artifact(artifact_id)["indexed"] is True

    closed = client.get("/vesi/" + quote(lookalike["id"]))
    assert closed.status_code == 200
    assert "Нет информации" in closed.text
    assert "Фамилии" not in closed.text
    assert "Списка фамилий пока нет." not in closed.text
    assert "/mogilki?cemetery=" not in closed.text
    assert get_artifact(lookalike["id"])["surnames"] == []
    assert get_artifact("нет-такого") is None
    assert client.get("/vesi/нет-такого").status_code == 404


def _placed_ids(client) -> set[str]:
    payload = client.get("/vesi/data").json()
    return {
        artifact["id"]
        for settlement in payload["settlements"]
        for artifact in settlement["artifacts"]
    }


def _sample_unbound() -> dict:
    return next(item for item in load_index()["unbound"] if item["has_point"])


def test_unbound_list_omits_artifacts_already_on_a_settlement(client):
    page = client.get("/vesi")
    assert page.status_code == 200
    assert "Без населённого пункта" in page.text
    assert "Привязать" not in page.text
    assert "парафия" in page.text
    script = client.get("/static/vesi.js")
    assert 'aria-hidden", "true"' in script.text or "aria-hidden" in script.text
    assert "🪦" in script.text
    assert "кладбище" in script.text
    assert "⛪" in script.text
    assert "парафия" in script.text
    assert "artifact.lat" not in script.text
    unbound_ids = set(re.findall(r'data-artifact-id="([^"]+)"', page.text))
    placed = _placed_ids(client)
    assert unbound_ids
    assert not (unbound_ids & placed)
    assert "новосады" in placed
    index = load_index()
    endings = ("кладбище", "полевое")
    assert not any(item["name"].casefold().endswith(endings) for item in index["settlements"])
    assert not any(item["name"] == "еврейское кладбище через забор" for item in index["settlements"])
    assert any(item["name"] == "Кладбище" for item in index["unbound"])
    payload = client.get("/vesi/data").json()["settlements"]
    fence = [
        item
        for item in payload
        if any(artifact["name"] == "еврейское кладбище через забор" for artifact in item["artifacts"])
    ]
    assert [item["name"] for item in fence] == ["Барановичи"]
    assert any(
        artifact["name"] == "Барановичи. Старое католическое кладбище" for artifact in fence[0]["artifacts"]
    )


def _drop_binding(client, artifact_id: str) -> None:
    db = client.app.state.session_factory()
    db.execute(delete(ArtifactSettlement).where(ArtifactSettlement.artifact_id == artifact_id))
    db.commit()
    db.close()


def test_nearest_suggestions_are_the_closest_settlements():
    index = load_index()
    artifact = _sample_unbound()
    suggestions = artifact["suggestions"]
    assert len(suggestions) == 4
    distances = sorted(
        (
            haversine_km(artifact["lat"], artifact["lon"], item["lat"], item["lon"]),
            item["id"],
        )
        for item in index["settlements"]
        if item["lat"] is not None and item["lon"] is not None
    )
    assert [item["id"] for item in suggestions] == [item_id for _distance, item_id in distances[:4]]


def test_saved_binding_is_not_replaced(client):
    row = saved_bindings()[0]
    db = client.app.state.session_factory()
    stored = db.get(ArtifactSettlement, row["artifact_id"])
    stored.settlement_id = "ruslan-choice"
    db.commit()
    ensure_artifact_settlements(db)
    db.expire_all()
    assert db.get(ArtifactSettlement, row["artifact_id"]).settlement_id == "ruslan-choice"
    db.close()


def test_non_ruslan_cannot_bind(client):
    artifact = _sample_unbound()
    _drop_binding(client, artifact["id"])
    settlement_id = artifact["suggestions"][0]["id"]
    guest = client.post(
        "/vesi/bind",
        data={"artifact_id": artifact["id"], "settlement_id": settlement_id, "username": "ruslan"},
        follow_redirects=False,
    )
    assert guest.status_code == 403
    login(client, "admin", "password123")
    denied = client.post(
        "/vesi/bind",
        data={"artifact_id": artifact["id"], "settlement_id": settlement_id, "username": "ruslan"},
        follow_redirects=False,
    )
    assert denied.status_code == 403
    assert artifact["id"] not in _placed_ids(client)
    page = client.get("/vesi")
    assert f'data-artifact-id="{artifact["id"]}"' in page.text
    assert "Привязать" not in page.text


def test_ruslan_can_bind_an_unbound_artifact(client):
    artifact = _sample_unbound()
    _drop_binding(client, artifact["id"])
    settlement_id = artifact["suggestions"][0]["id"]
    registered = client.post(
        "/register",
        data={"username": "ruslan", "password": "password123"},
        follow_redirects=False,
    )
    assert registered.status_code == 303
    page = client.get("/vesi")
    assert "Привязать" in page.text
    assert settlement_id in page.text
    bound = client.post(
        "/vesi/bind",
        data={"artifact_id": artifact["id"], "settlement_id": settlement_id},
        follow_redirects=False,
    )
    assert bound.status_code == 303
    settlements = client.get("/vesi/data").json()["settlements"]
    target = next(item for item in settlements if item["id"] == settlement_id)
    assert artifact["id"] in {item["id"] for item in target["artifacts"]}
    others = [
        item["id"]
        for settlement in settlements
        if settlement["id"] != settlement_id
        for item in settlement["artifacts"]
        if item["id"] == artifact["id"]
    ]
    assert others == []
    after = client.get("/vesi")
    assert f'data-artifact-id="{artifact["id"]}"' not in after.text


def test_verified_parish_opens_geneteka_and_counts(client):
    now = next(row for row in load_parishes() if row["name"] == "Nowogródek")
    assert now["status"] == "verified"
    payload = client.get("/vesi/data").json()["settlements"]
    settlement = next(item for item in payload if item["id"] == now["settlement_id"])
    parish = next(item for item in settlement["artifacts"] if item["id"] == "parish-" + now["id"])
    assert parish["kind"] == "parish"
    assert parish["label"] == "Свободный доступ"
    assert parish["emoji"] == "✅"
    assert parish["indexed"] is False
    assert parish["url"] == now["url"]
    assert "geneteka.genealodzy.pl" in parish["url"]
    births = next(item for item in parish["coverage"] if item["kind"] == "births")
    assert births["label"] == "рождения"
    assert births["years"] == now["births"]["years"]
    assert str(births["records"]) == str(now["births"]["records"])
    assert has_information(parish)
    others = [item for item in settlement["artifacts"] if item.get("kind") != "parish"]
    assert others
    assert all(not has_information(item) for item in others)
    assert marker_tone(others) == "red"
    assert marker_tone(settlement["artifacts"]) != "red"
    assert settlement["marker_class"] == marker_for(settlement["artifacts"])["marker_class"]
    page = client.get("/vesi/" + quote(parish["id"]))
    assert page.status_code == 200
    assert "парафия" in page.text
    assert "⛪" in page.text
    assert now["url"].replace("&", "&amp;") in page.text
    assert "рождения" in page.text
    assert "/mogilki?cemetery=" not in page.text
    assert "Фамилии" not in page.text
    gate = client.get("/mogilki")
    assert gate.status_code == 200
    assert "Пароль" in gate.text
    assert "geneteka.genealodzy.pl" not in gate.text


def test_unverified_parishes_stay_unbound(client):
    catalog = load_parishes()
    payload = client.get("/vesi/data").json()["settlements"]
    placed = {artifact["id"] for item in payload for artifact in item["artifacts"]}
    attached = []
    loose = []
    for row in catalog:
        artifact_id = "parish-" + str(row["id"])
        if artifact_id in placed:
            attached.append(row)
        else:
            loose.append(row)
    assert attached
    assert {row["status"] for row in attached} == {"verified"}
    assert {row["status"] for row in loose} <= {"ambiguous", "not_on_map", "cemetery_only"}
    assert len(attached) + len(loose) == len(catalog)
    grodno_points = [item for item in payload if item["name"] == "Гродно"]
    assert len(grodno_points) == 1
    grodno_names = {item["name"] for item in grodno_points[0]["artifacts"]}
    assert "Кладбище старое католическое (фарное)" in grodno_names
    assert "Кладбище старое католическое (занеманское)" in grodno_names
    assert "Подол" in grodno_names
    assert all(
        item.get("kind") != "parish" or str(item["id"]).startswith("prikhody-")
        for item in grodno_points[0]["artifacts"]
    )
    assert any(item["name"] == "Вертелишки" for item in payload)
    brest_points = [item for item in payload if item["name"] == "Брест"]
    assert len(brest_points) == 1
    assert any(item["name"] == "Тришинское кладбище" for item in brest_points[0]["artifacts"])
    assert any(item["name"] == "Черни" for item in payload)
    homel_points = [item for item in payload if item["name"] == "Гомель"]
    homel_parish = next(row for row in catalog if row["place"] == "Homel")
    assert homel_parish["status"] == "cemetery_only"
    assert all(
        "parish-" + homel_parish["id"] not in {item["id"] for item in point["artifacts"]}
        for point in homel_points
    )
    page = client.get("/vesi")
    adam = next(row for row in catalog if row["name"] == "Adamowicze")
    assert adam["status"] == "ambiguous"
    chunk = page.text.split(f'data-artifact-id="parish-{adam["id"]}"', 1)[1].split("data-artifact-id=", 1)[0]
    assert "парафия" in chunk
    assert "Координат нет." in chunk
    assert "Привязать" not in chunk
    grodno = next(row for row in catalog if row["place"] == "Grodno")
    assert grodno["status"] != "verified"
    assert f'data-artifact-id="parish-{grodno["id"]}"' in page.text
    assert f"parish-{grodno['id']}" not in placed


def test_parish_catalog_load_is_idempotent():
    assert len(load_parishes()) == len(load_parishes()) == 364
    assert len(load_prikhody()) == len(load_prikhody())
    first = build_index()
    second = build_index()

    def prefixed(index, prefix):
        return sorted(
            item["id"]
            for item in index["artifacts"].values()
            if item.get("kind") == "parish" and item["id"].startswith(prefix)
        )

    assert prefixed(first, "parish-") == prefixed(second, "parish-")
    assert len(prefixed(first, "parish-")) == 364
    assert prefixed(first, "prikhody-") == prefixed(second, "prikhody-")
    assert len(prefixed(first, "prikhody-")) == len(load_prikhody())
    settlements = {item["id"] for item in first["settlements"]}
    assert settlements == {item["id"] for item in second["settlements"]}
    geneteka_attached = [
        item
        for item in first["artifacts"].values()
        if item.get("kind") == "parish" and item["id"].startswith("parish-") and item["settlement_id"]
    ]
    assert len(geneteka_attached) == sum(1 for row in load_parishes() if row["status"] == "verified")
    prikhody_attached = [
        item
        for item in first["artifacts"].values()
        if item["id"].startswith("prikhody-") and item["settlement_id"]
    ]
    assert len(prikhody_attached) == len({item["id"] for item in prikhody_attached})
    located = {row["id"] for row in load_prikhody() if row["lat"] is not None and row["lon"] is not None}
    attached_ids = {item["id"].removeprefix("prikhody-") for item in prikhody_attached}
    assert located <= attached_ids
    assert set(row["id"] for row in load_prikhody()) - attached_ids <= {
        row["id"] for row in load_prikhody() if row["lat"] is None or row["lon"] is None
    }


def test_prikhody_attaches_every_parish_and_keeps_novomysh_documents(client):
    catalog = {row["id"]: row for row in load_prikhody()}
    novo = catalog["novomyshskiy_prihod"]
    index = load_index()
    placed = {
        artifact["id"]: settlement
        for settlement in index["settlements"]
        for artifact in settlement["artifacts"]
    }
    assert {"prikhody-" + row_id for row_id, row in catalog.items() if row["lat"] is not None} <= set(placed)
    loose = [item for item in index["unbound"] if item["id"].startswith("prikhody-")]
    assert loose
    assert all(item["has_point"] is False for item in loose)
    assert {item["id"].removeprefix("prikhody-") for item in loose} <= {
        row_id for row_id, row in catalog.items() if row["lat"] is None
    }
    added = [
        item
        for item in index["settlements"]
        if item["artifacts"] and all(str(artifact["id"]).startswith("prikhody-") for artifact in item["artifacts"])
    ]
    assert added
    assert not any(item["name"].casefold() in {"кладбище", "парафия", "приход"} for item in added)
    for settlement in added:
        distances = []
        for artifact in settlement["artifacts"]:
            row = catalog[artifact["id"].removeprefix("prikhody-")]
            if row["lat"] is None or row["lon"] is None:
                continue
            distances.append(haversine_km(row["lat"], row["lon"], settlement["lat"], settlement["lon"]))
        assert distances
        assert min(distances) < 0.05
    settlement = placed["prikhody-novomyshskiy_prihod"]
    assert settlement["name"] == "Новая Мышь"
    assert haversine_km(53.144115, 25.897292, settlement["lat"], settlement["lon"]) < 0.2
    km = haversine_km(novo["lat"], novo["lon"], settlement["lat"], settlement["lon"])
    assert km < 4
    parish = next(item for item in settlement["artifacts"] if item["id"] == "prikhody-novomyshskiy_prihod")
    assert parish["kind"] == "parish"
    assert parish["label"] == "Свободный доступ"
    assert parish["indexed"] is False
    assert parish["rite"] == "православный"
    assert parish["church"] == "церковь Преображения Господня"
    assert parish["url"] == novo["url"]
    assert len(parish["documents"]) == len(novo["documents"])
    assert any(doc["year"] == "1801–1812" and "НИАБ" in doc["archive"] for doc in parish["documents"])
    assert any("familysearch.org" in doc["url"] for doc in parish["documents"])
    assert any(doc.get("rejected") for doc in parish["documents"])
    assert "деревня Балабановичи" in parish["served"]
    assert any(item["id"] == "parish-3679" for item in settlement["artifacts"])
    page = client.get("/vesi/" + quote(parish["id"]))
    assert page.status_code == 200
    assert "1801–1812" in page.text
    assert "НИАБ" in page.text
    assert "Населённые пункты" in page.text
    assert "Балабановичи" in page.text
    assert "отказано в выдаче" in page.text
    assert "/mogilki?cemetery=" not in page.text
    gate = client.get("/mogilki")
    assert gate.status_code == 200
    assert "Пароль" in gate.text
