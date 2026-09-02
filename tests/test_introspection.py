import uuid

from fastapi.testclient import TestClient

from app.core.siris import SirisAI, get_siris


def test_list_tools_includes_full_detail(client: TestClient) -> None:
    response = client.get("/siris/tools")
    assert response.status_code == 200
    by_name = {entry["name"]: entry for entry in response.json()}

    assert "echo" in by_name
    assert by_name["echo"]["description"]
    assert "parameters" in by_name["echo"]
    assert by_name["echo"]["permission"] == "read"

    assert by_name["remember"]["permission"] == "safe_action"


def test_list_skills_only_returns_skill_tools(client: TestClient) -> None:
    response = client.get("/siris/skills")
    assert response.status_code == 200
    names = {entry["name"] for entry in response.json()}

    assert "server_health_check" in names
    assert "echo" not in names
    assert "remember" not in names


def test_capabilities_groups_by_category(client: TestClient) -> None:
    response = client.get("/siris/capabilities")
    assert response.status_code == 200
    body = response.json()

    assert "remember" in body["memory"]
    assert "recall_entity_relationships" in body["memory"]
    assert "server_health_check" in body["skills"]
    assert "echo" in body["misc"]
    # Every registered tool appears in exactly one category.
    all_listed = [name for names in body.values() for name in names]
    assert len(all_listed) == len(set(all_listed))


def test_system_state_reflects_world_model() -> None:
    siris = SirisAI(settings=get_siris().settings)
    subject, obj = f"Plex-{uuid.uuid4()}", f"Server-{uuid.uuid4()}"
    siris.entities.add_relationship(subject, "RUNS_ON", obj)

    state = siris.system_state()

    assert subject in state["entities"]
    assert obj in state["entities"]
    assert any(
        r["subject_entity"] == subject and r["object_entity"] == obj and r["relation"] == "RUNS_ON"
        for r in state["relationships"]
    )


def test_system_state_endpoint(client: TestClient) -> None:
    subject, obj = f"Plex-{uuid.uuid4()}", f"Server-{uuid.uuid4()}"
    remember = client.post(
        "/siris/tools/remember_entity_relationship/run",
        json={"arguments": {"subject_entity": subject, "relation": "RUNS_ON", "object_entity": obj}},
    )
    assert remember.status_code == 200

    response = client.get("/siris/system/state")
    assert response.status_code == 200
    body = response.json()
    assert subject in body["entities"]
    assert any(r["object_entity"] == obj for r in body["relationships"])


def test_entity_only_known_via_relationship_still_appears_in_all_entity_names() -> None:
    siris = SirisAI(settings=get_siris().settings)
    subject, obj = f"Plex-{uuid.uuid4()}", f"Server-{uuid.uuid4()}"
    # obj never gets an EntityFact -- only exists as a relationship target.
    siris.entities.add_relationship(subject, "RUNS_ON", obj)

    names = siris.entities.all_entity_names()
    assert subject in names
    assert obj in names
