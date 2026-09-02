import uuid

from fastapi.testclient import TestClient

from app.core.siris import SirisAI, get_siris


def test_entity_facts_accumulate_and_recall_most_recent_first() -> None:
    siris = SirisAI(settings=get_siris().settings)
    entity_name = f"TestPlex-{uuid.uuid4()}"

    siris.entities.remember_fact(entity_name, "runs on the media server")
    siris.entities.remember_fact(entity_name, "port 32400")

    facts = siris.entities.facts_about(entity_name)
    assert [f["fact"] for f in facts] == ["port 32400", "runs on the media server"]
    assert all(f["entity_name"] == entity_name for f in facts)


def test_list_entities_returns_distinct_names() -> None:
    siris = SirisAI(settings=get_siris().settings)
    name_a, name_b = f"A-{uuid.uuid4()}", f"B-{uuid.uuid4()}"
    siris.entities.remember_fact(name_a, "fact 1")
    siris.entities.remember_fact(name_a, "fact 2")
    siris.entities.remember_fact(name_b, "fact 3")

    entities = siris.entities.list_entities()
    assert name_a in entities
    assert name_b in entities
    assert entities.count(name_a) == 1


def test_events_filtered_by_entity() -> None:
    siris = SirisAI(settings=get_siris().settings)
    entity_name = f"TestPlex-{uuid.uuid4()}"
    other_name = f"Other-{uuid.uuid4()}"

    siris.events.log_event("container crashed", entity_name=entity_name)
    siris.events.log_event("container restarted", entity_name=entity_name)
    siris.events.log_event("unrelated event", entity_name=other_name)

    events = siris.events.recent_events(entity_name=entity_name)
    assert [e["description"] for e in events] == ["container restarted", "container crashed"]
    assert all(e["entity_name"] == entity_name for e in events)


def test_events_without_filter_returns_all_recent() -> None:
    siris = SirisAI(settings=get_siris().settings)
    marker = str(uuid.uuid4())
    siris.events.log_event(f"event {marker}")

    events = siris.events.recent_events(limit=100)
    assert any(e["description"] == f"event {marker}" for e in events)


def test_entity_and_event_tools_always_registered(client: TestClient) -> None:
    response = client.get("/siris/status")
    tools = response.json()["tools"]
    for name in ("remember_entity_fact", "recall_entity_facts", "log_event", "recall_events"):
        assert name in tools


def test_relationship_tools_always_registered(client: TestClient) -> None:
    response = client.get("/siris/status")
    tools = response.json()["tools"]
    for name in ("remember_entity_relationship", "recall_entity_relationships"):
        assert name in tools


def test_relationships_for_returns_outgoing_and_incoming_edges() -> None:
    siris = SirisAI(settings=get_siris().settings)
    plex, server, storage = f"Plex-{uuid.uuid4()}", f"Server-{uuid.uuid4()}", f"Storage-{uuid.uuid4()}"

    siris.entities.add_relationship(plex, "RUNS_ON", server)
    siris.entities.add_relationship(plex, "DEPENDS_ON", storage)
    siris.entities.add_relationship(server, "HOSTS", plex)

    plex_edges = siris.entities.relationships_for(plex)
    assert {(e["relation"], e["object_entity"]) for e in plex_edges["outgoing"]} == {
        ("RUNS_ON", server),
        ("DEPENDS_ON", storage),
    }
    assert {(e["subject_entity"], e["relation"]) for e in plex_edges["incoming"]} == {(server, "HOSTS")}

    # The reverse lookup this milestone is for: "what would shutting down
    # storage affect" is answered by storage's incoming edges.
    storage_edges = siris.entities.relationships_for(storage)
    assert storage_edges["outgoing"] == []
    assert [e["subject_entity"] for e in storage_edges["incoming"]] == [plex]


def test_relationships_for_unknown_entity_is_empty() -> None:
    siris = SirisAI(settings=get_siris().settings)
    edges = siris.entities.relationships_for(f"Nobody-{uuid.uuid4()}")
    assert edges == {"outgoing": [], "incoming": []}


def test_chat_can_record_and_recall_a_relationship_via_tools(client: TestClient) -> None:
    subject, obj = f"Plex-{uuid.uuid4()}", f"Server-{uuid.uuid4()}"

    remember = client.post(
        "/siris/tools/remember_entity_relationship/run",
        json={"arguments": {"subject_entity": subject, "relation": "RUNS_ON", "object_entity": obj}},
    )
    assert remember.status_code == 200

    recall = client.post(
        "/siris/tools/recall_entity_relationships/run",
        json={"arguments": {"entity_name": subject}},
    )
    assert recall.status_code == 200
    result = recall.json()["tool_call"]["result"]
    assert result["outgoing"][0]["relation"] == "RUNS_ON"
    assert result["outgoing"][0]["object_entity"] == obj
