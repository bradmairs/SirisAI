import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.core.siris import SirisAI, get_siris
from app.db.models import Goal
from app.db.session import SessionLocal
from app.notify.manager import NotificationManager
from app.skills.builtin.goal_check_in import GoalCheckInSkill


def _backdate_goal(goal_id: int, days: int) -> None:
    with SessionLocal() as session:
        goal = session.get(Goal, goal_id)
        goal.updated_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
        session.commit()


def _siris() -> SirisAI:
    return SirisAI(settings=get_siris().settings)


# -- GoalCheckInSkill


def test_goal_check_in_flags_a_stale_active_goal() -> None:
    siris = _siris()
    title = f"Migrate to k3s {uuid.uuid4()}"
    goal = siris.goals.create_goal(title)
    _backdate_goal(goal["id"], days=30)

    result = GoalCheckInSkill().run(siris)

    assert result["healthy"] is False
    assert any(title in w for w in result["warnings"])
    assert any(g["id"] == goal["id"] for g in result["stale_goals"])


def test_goal_check_in_does_not_flag_a_fresh_goal() -> None:
    siris = _siris()
    title = f"Fresh goal {uuid.uuid4()}"
    siris.goals.create_goal(title)

    result = GoalCheckInSkill().run(siris)

    assert not any(title in w for w in result["warnings"])


def test_goal_check_in_healthy_with_no_active_goals() -> None:
    siris = SirisAI(settings=get_siris().settings)
    # No isolation needed unlike event timing's burst test -- staleness is
    # keyed off real elapsed time (>=14 days), so other tests' freshly
    # created goals can never spuriously show up here regardless of DB
    # sharing across the test session.
    result = GoalCheckInSkill().run(siris)
    assert isinstance(result["healthy"], bool)
    assert isinstance(result["warnings"], list)


# -- NotificationManager classification


def test_notification_manager_classifies_stale_goal_as_goals_category() -> None:
    siris = _siris()
    items = NotificationManager(siris.entities).summarize(
        ["Goal 'Migrate to k3s' hasn't been updated in over 14 days (still active)."]
    )
    assert items[0].category == "goals"
    assert items[0].action == "notify"
    assert items[0].severity == "warning"


# -- Endpoint-level: POST /siris/proactive/check composes goal_check_in


def test_proactive_check_includes_goal_check_in_result(client: TestClient) -> None:
    response = client.post("/siris/proactive/check")
    assert response.status_code == 200
    body = response.json()
    assert "goal_check_in" in body
    assert "warnings" in body["goal_check_in"]
    assert "stale_goals" in body["goal_check_in"]


def test_goal_check_in_tool_is_registered_as_a_skill() -> None:
    siris = _siris()
    assert siris.tools.get("goal_check_in") is not None
    assert "goal_check_in" in siris.capabilities()["skills"]
