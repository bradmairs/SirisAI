import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.core.siris import SirisAI, get_siris
from app.db.models import Goal
from app.db.session import SessionLocal


def _backdate_goal(goal_id: int, days: int) -> None:
    # Same SQLite-round-trip-drops-tzinfo normalization as everywhere else
    # here (see RoutineStore.due_routines()'s docstring for the full
    # story) -- only for constructing a controlled "already stale" fixture.
    with SessionLocal() as session:
        goal = session.get(Goal, goal_id)
        goal.updated_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
        session.commit()


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


# -- stale_goals() (a hardening pass, milestone 40)


def test_stale_goals_excludes_a_recently_updated_goal() -> None:
    siris = SirisAI(settings=get_siris().settings)
    goal = siris.goals.create_goal(f"Fresh goal {uuid.uuid4()}")
    assert not any(g["id"] == goal["id"] for g in siris.goals.stale_goals(days=14))


def test_stale_goals_includes_an_untouched_old_goal() -> None:
    siris = SirisAI(settings=get_siris().settings)
    goal = siris.goals.create_goal(f"Old goal {uuid.uuid4()}")
    _backdate_goal(goal["id"], days=30)

    assert any(g["id"] == goal["id"] for g in siris.goals.stale_goals(days=14))


def test_stale_goals_excludes_a_non_active_goal_even_if_old() -> None:
    siris = SirisAI(settings=get_siris().settings)
    goal = siris.goals.create_goal(f"Abandoned goal {uuid.uuid4()}")
    siris.goals.update_goal_status(goal["id"], "abandoned")
    _backdate_goal(goal["id"], days=30)

    assert not any(g["id"] == goal["id"] for g in siris.goals.stale_goals(days=14))


def test_add_objective_un_stales_a_goal() -> None:
    """The actual bug this milestone fixed: adding an objective is real
    progress on a goal, but it's a GoalObjective row, not a write to the
    Goal row itself -- without explicitly bumping updated_at, a goal
    getting steady objective work would look untouched for weeks."""
    siris = SirisAI(settings=get_siris().settings)
    goal = siris.goals.create_goal(f"Actively worked goal {uuid.uuid4()}")
    _backdate_goal(goal["id"], days=30)
    assert any(g["id"] == goal["id"] for g in siris.goals.stale_goals(days=14))

    siris.goals.add_objective(goal["id"], "New step")

    assert not any(g["id"] == goal["id"] for g in siris.goals.stale_goals(days=14))


def test_complete_objective_un_stales_a_goal() -> None:
    siris = SirisAI(settings=get_siris().settings)
    goal = siris.goals.create_goal(f"Actively worked goal {uuid.uuid4()}", objectives=["Only step"])
    _backdate_goal(goal["id"], days=30)
    assert any(g["id"] == goal["id"] for g in siris.goals.stale_goals(days=14))

    siris.goals.complete_objective(goal["objectives"][0]["id"])

    assert not any(g["id"] == goal["id"] for g in siris.goals.stale_goals(days=14))


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


# -- Performance hardening pass: list_goals()/stale_goals() used to run
# 1 + 2*N queries for N goals (an id-only query, then a fresh
# session.get() plus a fresh objectives query per goal -- a classic
# N+1). They now run 2 total, regardless of N.


def _count_queries(fn: object, *args: object, **kwargs: object) -> tuple[object, int]:
    from sqlalchemy import event

    from app.db.session import engine

    count = 0

    def _on_execute(*_: object, **__: object) -> None:
        nonlocal count
        count += 1

    event.listen(engine, "before_cursor_execute", _on_execute)
    try:
        result = fn(*args, **kwargs)  # type: ignore[operator]
    finally:
        event.remove(engine, "before_cursor_execute", _on_execute)
    return result, count


def test_list_goals_query_count_does_not_scale_with_goal_count() -> None:
    siris = SirisAI(settings=get_siris().settings)
    marker = str(uuid.uuid4())
    for i in range(5):
        siris.goals.create_goal(f"N+1 check {marker} {i}", objectives=["a", "b", "c"])

    _, query_count_before = _count_queries(siris.goals.list_goals)
    for i in range(5, 10):
        siris.goals.create_goal(f"N+1 check {marker} {i}", objectives=["a", "b", "c"])
    _, query_count_after = _count_queries(siris.goals.list_goals)

    # More goals in the table (including from other tests, since this is
    # the shared test DB) changes row counts, not query counts -- both
    # measurements should be the same small constant regardless.
    assert query_count_before == query_count_after
    assert query_count_before <= 3  # generous headroom over the real count (2)


def test_list_goals_correctly_groups_objectives_per_goal_after_batching() -> None:
    """The actual risk in batching objectives across goals in one query
    (see GoalStore._serialize_goals()): mis-grouping one goal's
    objectives under another. Distinct, checkable objective text per
    goal catches that."""
    siris = SirisAI(settings=get_siris().settings)
    marker = str(uuid.uuid4())
    first = siris.goals.create_goal(f"First {marker}", objectives=["first-a", "first-b"])
    second = siris.goals.create_goal(f"Second {marker}", objectives=["second-a"])
    third = siris.goals.create_goal(f"Third {marker}")  # no objectives at all

    listed = {g["id"]: g for g in siris.goals.list_goals()}
    assert [o["description"] for o in listed[first["id"]]["objectives"]] == ["first-a", "first-b"]
    assert [o["description"] for o in listed[second["id"]]["objectives"]] == ["second-a"]
    assert listed[third["id"]]["objectives"] == []
