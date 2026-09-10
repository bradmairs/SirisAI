import uuid
from typing import Any

from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.plugins.base import ToolPermission
from app.skills.builtin.run_scene import RunSceneSkill


def _fresh_siris() -> SirisAI:
    return SirisAI(settings=get_siris().settings)


def _siris_with_home_assistant() -> SirisAI:
    return SirisAI(
        settings=Settings(
            home_assistant_base_url="http://ha.local:8123",
            home_assistant_token="secret",
        )
    )


# -- SceneStore


def test_create_scene_returns_expected_shape() -> None:
    siris = _fresh_siris()
    name = f"scene-{uuid.uuid4()}"
    steps = [{"domain": "light", "service": "turn_off", "entity_id": "light.living_room"}]

    scene = siris.scenes.create_scene(name, steps)

    assert scene["name"] == name
    assert scene["steps"] == steps
    assert isinstance(scene["id"], int)
    assert scene["created_at"]


def test_list_scenes_includes_created_scene() -> None:
    siris = _fresh_siris()
    name = f"scene-{uuid.uuid4()}"
    created = siris.scenes.create_scene(name, [])

    ids = {s["id"] for s in siris.scenes.list_scenes()}
    assert created["id"] in ids


def test_get_scene_returns_none_for_unknown_id() -> None:
    siris = _fresh_siris()
    assert siris.scenes.get_scene(999999999) is None


def test_get_scene_round_trips_steps() -> None:
    siris = _fresh_siris()
    steps = [
        {"domain": "light", "service": "turn_off", "entity_id": "light.a"},
        {"domain": "media_player", "service": "turn_on", "entity_id": "media_player.tv"},
    ]
    created = siris.scenes.create_scene(f"scene-{uuid.uuid4()}", steps)

    fetched = siris.scenes.get_scene(created["id"])

    assert fetched is not None
    assert fetched["steps"] == steps


def test_delete_scene_returns_true_and_removes_it() -> None:
    siris = _fresh_siris()
    created = siris.scenes.create_scene(f"scene-{uuid.uuid4()}", [])

    assert siris.scenes.delete_scene(created["id"]) is True
    assert siris.scenes.get_scene(created["id"]) is None


def test_delete_scene_returns_false_for_unknown_id() -> None:
    siris = _fresh_siris()
    assert siris.scenes.delete_scene(999999999) is False


# -- Tools: registration and permission tiers


def test_scene_tools_not_registered_without_home_assistant() -> None:
    siris = SirisAI(settings=Settings())
    names = {tool.name for tool in siris.tools.list()}
    assert not names & {"create_scene", "list_scenes", "delete_scene", "run_scene"}


def test_scene_tools_registered_with_expected_permissions_when_home_assistant_configured() -> None:
    siris = _siris_with_home_assistant()
    try:
        create_tool = siris.tools.get("create_scene")
        list_tool = siris.tools.get("list_scenes")
        delete_tool = siris.tools.get("delete_scene")
        run_tool = siris.tools.get("run_scene")

        assert create_tool is not None and create_tool.permission is ToolPermission.SAFE_ACTION
        assert list_tool is not None and list_tool.permission is ToolPermission.READ
        assert delete_tool is not None and delete_tool.permission is ToolPermission.SAFE_ACTION
        assert run_tool is not None and run_tool.permission is ToolPermission.CONFIRM_REQUIRED
    finally:
        assert siris._home_assistant_client is not None
        siris._home_assistant_client.close()


def test_create_and_list_tools_round_trip() -> None:
    siris = _siris_with_home_assistant()
    try:
        name = f"scene-{uuid.uuid4()}"
        steps = [{"domain": "light", "service": "turn_on", "entity_id": "light.a"}]

        created = siris.run_tool("create_scene", {"name": name, "steps": steps})
        listed = siris.run_tool("list_scenes", {})

        assert any(s["id"] == created["id"] for s in listed)
    finally:
        assert siris._home_assistant_client is not None
        siris._home_assistant_client.close()


def test_delete_tool_removes_scene() -> None:
    siris = _siris_with_home_assistant()
    try:
        created = siris.run_tool(
            "create_scene", {"name": f"scene-{uuid.uuid4()}", "steps": []}
        )

        result = siris.run_tool("delete_scene", {"scene_id": created["id"]})

        assert result == {"deleted": True}
        assert siris.scenes.get_scene(created["id"]) is None
    finally:
        assert siris._home_assistant_client is not None
        siris._home_assistant_client.close()


# -- RunSceneSkill


class _FakeSceneStore:
    def __init__(self, scenes: dict[int, dict[str, Any]]) -> None:
        self._scenes = scenes

    def get_scene(self, scene_id: int) -> dict[str, Any] | None:
        return self._scenes.get(scene_id)


class _FakeSirisForRunScene:
    def __init__(self, scenes: dict[int, dict[str, Any]]) -> None:
        self.scenes = _FakeSceneStore(scenes)
        self.calls: list[dict[str, Any]] = []

    def run_tool(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        self.calls.append({"name": name, "arguments": arguments, **kwargs})
        return {"ok": True, "domain": arguments["domain"], "service": arguments["service"]}


def test_run_raises_for_unknown_scene_id() -> None:
    siris = _FakeSirisForRunScene({})
    try:
        RunSceneSkill().run(siris, scene_id=1)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_run_calls_home_assistant_call_service_once_per_step_in_order() -> None:
    steps = [
        {"domain": "light", "service": "turn_off", "entity_id": "light.a", "service_data": None},
        {"domain": "media_player", "service": "turn_on", "entity_id": "media_player.tv"},
    ]
    scene = {"id": 1, "name": "movie_night", "steps": steps}
    siris = _FakeSirisForRunScene({1: scene})

    result = RunSceneSkill().run(siris, scene_id=1)

    assert len(siris.calls) == 2
    assert siris.calls[0]["name"] == "home_assistant_call_service"
    assert siris.calls[0]["arguments"] == {
        "domain": "light",
        "service": "turn_off",
        "entity_id": "light.a",
        "service_data": None,
    }
    assert siris.calls[0]["confirmed"] is True
    assert siris.calls[1]["arguments"]["domain"] == "media_player"
    assert result["scene_id"] == 1
    assert result["name"] == "movie_night"
    assert len(result["step_results"]) == 2


def test_dry_run_previews_steps_without_calling_home_assistant() -> None:
    steps = [{"domain": "light", "service": "turn_off", "entity_id": "light.a"}]
    scene = {"id": 1, "name": "movie_night", "steps": steps}
    siris = _FakeSirisForRunScene({1: scene})

    result = RunSceneSkill().dry_run(siris, scene_id=1)

    assert result == {"scene_id": 1, "name": "movie_night", "steps": steps, "found": True}
    assert siris.calls == []


def test_dry_run_reports_not_found_for_unknown_scene_id() -> None:
    siris = _FakeSirisForRunScene({})

    result = RunSceneSkill().dry_run(siris, scene_id=1)

    assert result == {"scene_id": 1, "name": None, "steps": [], "found": False}


def test_verify_raises_not_implemented() -> None:
    siris = _FakeSirisForRunScene({})
    try:
        RunSceneSkill().verify(siris, {}, {})
        raise AssertionError("expected NotImplementedError")
    except NotImplementedError:
        pass
