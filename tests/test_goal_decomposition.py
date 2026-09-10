import uuid

from fastapi.testclient import TestClient

from app.core.siris import SirisAI, get_siris
from app.notify.manager import NotificationManager
from app.skills.builtin.goal_decomposition import GoalDecompositionCheckSkill


def _siris() -> SirisAI:
    return SirisAI(settings=get_siris().settings)


# -- GoalStore.goals_needing_decomposition


def test_goal_with_no_objectives_is_flagged() -> None:
    siris = _siris()
    title = f"Learn Spanish {uuid.uuid4()}"
    goal = siris.goals.create_goal(title)

    needing = siris.goals.goals_needing_decomposition()

    assert any(g["id"] == goal["id"] for g in needing)


def test_goal_with_an_objective_is_not_flagged() -> None:
    siris = _siris()
    title = f"Migrate to k3s {uuid.uuid4()}"
    goal = siris.goals.create_goal(title, ["Pick a distro"])

    needing = siris.goals.goals_needing_decomposition()

    assert not any(g["id"] == goal["id"] for g in needing)


def test_completed_goal_with_no_objectives_is_not_flagged() -> None:
    siris = _siris()
    title = f"Abandoned idea {uuid.uuid4()}"
    goal = siris.goals.create_goal(title)
    siris.goals.update_goal_status(goal["id"], "done")

    needing = siris.goals.goals_needing_decomposition()

    assert not any(g["id"] == goal["id"] for g in needing)


def test_goal_no_longer_flagged_once_an_objective_is_added() -> None:
    siris = _siris()
    title = f"Plan trip {uuid.uuid4()}"
    goal = siris.goals.create_goal(title)
    assert any(g["id"] == goal["id"] for g in siris.goals.goals_needing_decomposition())

    siris.goals.add_objective(goal["id"], "Pick a destination")

    assert not any(g["id"] == goal["id"] for g in siris.goals.goals_needing_decomposition())


# -- GoalDecompositionCheckSkill


def test_skill_flags_a_goal_with_no_objectives() -> None:
    siris = _siris()
    title = f"Learn Spanish {uuid.uuid4()}"
    goal = siris.goals.create_goal(title)

    result = GoalDecompositionCheckSkill().run(siris)

    assert result["healthy"] is False
    assert any(title in w for w in result["warnings"])
    assert any(g["id"] == goal["id"] for g in result["goals_needing_decomposition"])


def test_skill_does_not_flag_a_goal_with_objectives() -> None:
    siris = _siris()
    title = f"Migrate to k3s {uuid.uuid4()}"
    siris.goals.create_goal(title, ["Pick a distro"])

    result = GoalDecompositionCheckSkill().run(siris)

    assert not any(title in w for w in result["warnings"])


# -- NotificationManager classification


def test_notification_manager_classifies_decomposition_warning_as_goals_category() -> None:
    siris = _siris()
    items = NotificationManager(siris.entities).summarize(
        ["Goal 'Learn Spanish' has no objectives yet -- break it down into concrete next steps."]
    )
    assert items[0].category == "goals"
    assert items[0].action == "notify"
    assert items[0].severity == "warning"


# -- Endpoint-level: POST /siris/proactive/check composes goal_decomposition_check


def test_proactive_check_includes_goal_decomposition_result(client: TestClient) -> None:
    response = client.post("/siris/proactive/check")
    assert response.status_code == 200
    body = response.json()
    assert "goal_decomposition" in body
    assert "warnings" in body["goal_decomposition"]
    assert "goals_needing_decomposition" in body["goal_decomposition"]


def test_goal_decomposition_check_tool_is_registered_as_a_skill() -> None:
    siris = _siris()
    assert siris.tools.get("goal_decomposition_check") is not None
    assert "goal_decomposition_check" in siris.capabilities()["skills"]
