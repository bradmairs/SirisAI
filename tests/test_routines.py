import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient

from app.core.siris import SirisAI, get_siris
from app.routines.runner import RoutineRunner


def test_create_routine_and_list() -> None:
    siris = SirisAI(settings=get_siris().settings)
    name = f"nightly-check-{uuid.uuid4()}"

    routine = siris.routines.create_routine(name, "nightly", 1440, ["server_health_check"])
    assert routine["name"] == name
    assert routine["interval_minutes"] == 1440
    assert routine["actions"] == ["server_health_check"]
    assert routine["enabled"] is True
    assert routine["last_run_at"] is None

    listed = siris.routines.list_routines()
    assert any(r["id"] == routine["id"] for r in listed)


def test_set_enabled_toggles_and_unknown_id_returns_none() -> None:
    siris = SirisAI(settings=get_siris().settings)
    routine = siris.routines.create_routine(f"r-{uuid.uuid4()}", "hourly", 60, ["echo"])

    disabled = siris.routines.set_enabled(routine["id"], False)
    assert disabled["enabled"] is False

    assert siris.routines.set_enabled(999_999, False) is None


def test_enabled_only_filters_list() -> None:
    siris = SirisAI(settings=get_siris().settings)
    marker = str(uuid.uuid4())
    on = siris.routines.create_routine(f"on-{marker}", "hourly", 60, ["echo"])
    off = siris.routines.create_routine(f"off-{marker}", "hourly", 60, ["echo"])
    siris.routines.set_enabled(off["id"], False)

    enabled_ids = {r["id"] for r in siris.routines.list_routines(enabled_only=True)}
    assert on["id"] in enabled_ids
    assert off["id"] not in enabled_ids


def test_never_run_routine_is_due() -> None:
    siris = SirisAI(settings=get_siris().settings)
    routine = siris.routines.create_routine(f"r-{uuid.uuid4()}", "hourly", 60, ["echo"])

    due_ids = {r["id"] for r in siris.routines.due_routines()}
    assert routine["id"] in due_ids


def test_recently_run_routine_is_not_due() -> None:
    siris = SirisAI(settings=get_siris().settings)
    routine = siris.routines.create_routine(f"r-{uuid.uuid4()}", "hourly", 60, ["echo"])
    siris.routines.mark_run(routine["id"], run_at=datetime.now(UTC))

    due_ids = {r["id"] for r in siris.routines.due_routines()}
    assert routine["id"] not in due_ids


def test_routine_becomes_due_once_interval_elapses() -> None:
    siris = SirisAI(settings=get_siris().settings)
    routine = siris.routines.create_routine(f"r-{uuid.uuid4()}", "hourly", 60, ["echo"])
    siris.routines.mark_run(routine["id"], run_at=datetime.now(UTC) - timedelta(minutes=61))

    due_ids = {r["id"] for r in siris.routines.due_routines()}
    assert routine["id"] in due_ids


def test_disabled_routine_is_never_due() -> None:
    siris = SirisAI(settings=get_siris().settings)
    routine = siris.routines.create_routine(f"r-{uuid.uuid4()}", "hourly", 60, ["echo"])
    siris.routines.set_enabled(routine["id"], False)

    due_ids = {r["id"] for r in siris.routines.due_routines()}
    assert routine["id"] not in due_ids


def test_create_routine_tool_rejects_unknown_action(client: TestClient) -> None:
    response = client.post(
        "/siris/tools/create_routine/run",
        json={
            "arguments": {
                "name": f"bad-{uuid.uuid4()}",
                "trigger_description": "hourly",
                "interval_minutes": 60,
                "actions": ["this_tool_does_not_exist"],
            }
        },
    )
    assert response.status_code == 502


def test_create_routine_tool_rejects_non_positive_interval(client: TestClient) -> None:
    response = client.post(
        "/siris/tools/create_routine/run",
        json={
            "arguments": {
                "name": f"bad-{uuid.uuid4()}",
                "trigger_description": "hourly",
                "interval_minutes": 0,
                "actions": ["echo"],
            }
        },
    )
    assert response.status_code == 502


def test_routine_tools_always_registered(client: TestClient) -> None:
    tools = client.get("/siris/status").json()["tools"]
    for name in ("create_routine", "list_routines", "set_routine_enabled"):
        assert name in tools


# -- RoutineRunner


class _FakeSirisForRunner:
    def __init__(self) -> None:
        self.routines = self
        self.run_tool_calls: list[tuple[str, str]] = []  # (tool_name, trigger)
        self._due: list[dict[str, Any]] = []
        self.marked_run: list[int] = []

    def due_routines(self) -> list[dict[str, Any]]:
        return self._due

    def mark_run(self, routine_id: int) -> None:
        self.marked_run.append(routine_id)

    def run_tool(self, name: str, arguments: dict[str, Any], trigger: str | None = None) -> Any:
        self.run_tool_calls.append((name, trigger or ""))
        if name == "flaky_tool":
            raise RuntimeError("boom")
        return {"ok": name}


def test_runner_executes_each_action_and_marks_run() -> None:
    siris = _FakeSirisForRunner()
    siris._due = [{"id": 1, "name": "nightly", "trigger_description": "nightly", "actions": ["a", "b"]}]

    results = RoutineRunner(siris).run_due()

    assert len(results) == 1
    assert results[0]["routine_id"] == 1
    assert [a["tool"] for a in results[0]["actions"]] == ["a", "b"]
    assert [a["result"] for a in results[0]["actions"]] == [{"ok": "a"}, {"ok": "b"}]
    assert siris.marked_run == [1]
    assert all(trigger == "routine: 'nightly' (nightly)" for _, trigger in siris.run_tool_calls)


def test_runner_records_a_failed_action_without_stopping_the_rest() -> None:
    siris = _FakeSirisForRunner()
    siris._due = [{"id": 2, "name": "check", "trigger_description": "hourly", "actions": ["flaky_tool", "ok_tool"]}]

    results = RoutineRunner(siris).run_due()

    actions = results[0]["actions"]
    assert actions[0]["error"] == "boom"
    assert actions[0]["result"] is None
    assert actions[1]["error"] is None
    assert actions[1]["result"] == {"ok": "ok_tool"}
    assert siris.marked_run == [2]  # still marked run even though a step failed


def test_run_due_endpoint_actually_runs_a_due_routine(client: TestClient) -> None:
    fake_siris = get_siris()
    name = f"endpoint-check-{uuid.uuid4()}"
    routine = fake_siris.routines.create_routine(name, "every minute", 1, ["echo"])

    response = client.post("/siris/routines/run_due")
    assert response.status_code == 200
    body = response.json()
    matching = next(r for r in body if r["routine_id"] == routine["id"])
    assert matching["name"] == name
    assert matching["actions"][0]["tool"] == "echo"

    listed = client.get("/siris/routines").json()
    ran = next(r for r in listed if r["id"] == routine["id"])
    assert ran["last_run_at"] is not None

    # Immediately due again? No -- just ran, interval is 1 minute, hasn't elapsed.
    second_run = client.post("/siris/routines/run_due").json()
    assert not any(r["routine_id"] == routine["id"] for r in second_run)
