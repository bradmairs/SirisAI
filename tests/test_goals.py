import uuid

from fastapi.testclient import TestClient

from app.core.siris import SirisAI, get_siris


def test_create_goal_with_objectives() -> None:
    siris = SirisAI(settings=get_siris().settings)
    title = f"Reduce power consumption {uuid.uuid4()}"

    goal = siris.goals.create_goal(title, objectives=["Identify high-power devices", "Monitor idle periods"])

    assert goal["title"] == title
    assert goal["status"] == "active"
    assert [o["description"] for o in goal["objectives"]] == [
        "Identify high-power devices",
        "Monitor idle periods",
    ]
    assert all(o["done"] is False for o in goal["objectives"])


def test_create_goal_without_objectives() -> None:
    siris = SirisAI(settings=get_siris().settings)
    goal = siris.goals.create_goal(f"Bare goal {uuid.uuid4()}")
    assert goal["objectives"] == []


def test_list_goals_filters_by_status_and_orders_newest_first() -> None:
    siris = SirisAI(settings=get_siris().settings)
    marker = str(uuid.uuid4())
    first = siris.goals.create_goal(f"First {marker}")
    second = siris.goals.create_goal(f"Second {marker}")
    siris.goals.update_goal_status(first["id"], "abandoned")

    active = siris.goals.list_goals(status="active")
    active_titles = [g["title"] for g in active if marker in g["title"]]
    assert active_titles == [f"Second {marker}"]

    abandoned = siris.goals.list_goals(status="abandoned")
    abandoned_titles = [g["title"] for g in abandoned if marker in g["title"]]
    assert abandoned_titles == [f"First {marker}"]

    all_goals = siris.goals.list_goals()
    all_titles = [g["title"] for g in all_goals if marker in g["title"]]
    assert set(all_titles) == {f"First {marker}", f"Second {marker}"}
    assert second["id"] in [g["id"] for g in all_goals]


def test_update_goal_status_unknown_goal_returns_none() -> None:
    siris = SirisAI(settings=get_siris().settings)
    assert siris.goals.update_goal_status(999_999, "completed") is None


def test_add_objective_and_complete_objective() -> None:
    siris = SirisAI(settings=get_siris().settings)
    goal = siris.goals.create_goal(f"Goal {uuid.uuid4()}", objectives=["Step one"])

    updated = siris.goals.add_objective(goal["id"], "Step two")
    assert [o["description"] for o in updated["objectives"]] == ["Step one", "Step two"]

    step_one_id = updated["objectives"][0]["id"]
    completed = siris.goals.complete_objective(step_one_id)
    done_map = {o["id"]: o["done"] for o in completed["objectives"]}
    assert done_map[step_one_id] is True
    assert done_map[updated["objectives"][1]["id"]] is False


def test_complete_objective_unknown_id_returns_none() -> None:
    siris = SirisAI(settings=get_siris().settings)
    assert siris.goals.complete_objective(999_999) is None


def test_completing_all_objectives_does_not_auto_complete_goal() -> None:
    siris = SirisAI(settings=get_siris().settings)
    goal = siris.goals.create_goal(f"Goal {uuid.uuid4()}", objectives=["Only step"])
    siris.goals.complete_objective(goal["objectives"][0]["id"])

    reloaded = siris.goals.list_goals()
    matching = next(g for g in reloaded if g["id"] == goal["id"])
    assert matching["status"] == "active"


def test_goal_tools_always_registered(client: TestClient) -> None:
    response = client.get("/siris/status")
    tools = response.json()["tools"]
    for name in (
        "create_goal",
        "list_goals",
        "update_goal_status",
        "add_goal_objective",
        "complete_goal_objective",
    ):
        assert name in tools


def test_create_and_list_goal_via_tools_endpoint(client: TestClient) -> None:
    title = f"Tool-created goal {uuid.uuid4()}"
    create = client.post(
        "/siris/tools/create_goal/run",
        json={"arguments": {"title": title, "objectives": ["Do the thing"]}},
    )
    assert create.status_code == 200
    goal_id = create.json()["tool_call"]["result"]["id"]

    listed = client.get("/siris/goals")
    assert listed.status_code == 200
    assert any(g["id"] == goal_id and g["title"] == title for g in listed.json())


def test_update_goal_status_unknown_id_returns_502(client: TestClient) -> None:
    response = client.post(
        "/siris/tools/update_goal_status/run",
        json={"arguments": {"goal_id": 999_999, "status": "completed"}},
    )
    assert response.status_code == 502
