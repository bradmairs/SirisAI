import httpx
import pytest

from app.core.siris import SirisAI, get_siris
from app.home_assistant.client import HomeAssistantClient
from app.plugins.base import ConfirmationRequired, ToolPermission
from app.plugins.builtin.home_assistant import HomeAssistantCallServiceTool
from app.plugins.builtin.radarr import RadarrAddMovieTool, RadarrDeleteMovieTool
from app.plugins.builtin.sonarr import SonarrAddSeriesTool, SonarrDeleteSeriesTool
from app.radarr.client import RadarrClient
from app.skills.builtin.rollback import RollbackLastActionSkill
from app.sonarr.client import SonarrClient


def _siris() -> SirisAI:
    return SirisAI(settings=get_siris().settings)


def test_rollback_tool_is_registered_and_confirm_required() -> None:
    siris = _siris()
    tool = siris.tools.get("rollback_last_action")
    assert tool is not None
    assert tool.permission is ToolPermission.CONFIRM_REQUIRED


def test_rollback_reverses_a_radarr_add_movie() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(
                201, json={"id": 55, "title": "Dune", "year": 2021, "monitored": True}
            )
        return httpx.Response(200, json={})

    client = RadarrClient(
        base_url="http://radarr.local",
        api_key="key",
        transport=httpx.MockTransport(handler),
    )
    siris = _siris()
    siris.tools.register(RadarrAddMovieTool(client))
    siris.tools.register(RadarrDeleteMovieTool(client))

    siris.run_tool(
        "radarr_add_movie",
        {
            "tmdb_id": 1,
            "title": "Dune",
            "quality_profile_id": 4,
            "root_folder_path": "/movies",
        },
        confirmed=True,
    )

    result = RollbackLastActionSkill().run(siris, tool_name="radarr_add_movie")

    assert result["rolled_back"] is True
    assert result["undo_tool"] == "radarr_delete_movie"
    assert result["undo_arguments"] == {"movie_id": 55}
    assert ("DELETE", "/api/v3/movie/55") in calls
    client.close()


def test_rollback_reverses_a_sonarr_add_series() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(
                201,
                json={"id": 77, "title": "Severance", "year": 2022, "monitored": True},
            )
        return httpx.Response(200, json={})

    client = SonarrClient(
        base_url="http://sonarr.local",
        api_key="key",
        transport=httpx.MockTransport(handler),
    )
    siris = _siris()
    siris.tools.register(SonarrAddSeriesTool(client))
    siris.tools.register(SonarrDeleteSeriesTool(client))

    siris.run_tool(
        "sonarr_add_series",
        {
            "tvdb_id": 1,
            "title": "Severance",
            "quality_profile_id": 4,
            "root_folder_path": "/tv",
        },
        confirmed=True,
    )

    result = RollbackLastActionSkill().run(siris, tool_name="sonarr_add_series")

    assert result["rolled_back"] is True
    assert result["undo_tool"] == "sonarr_delete_series"
    assert result["undo_arguments"] == {"series_id": 77}
    assert ("DELETE", "/api/v3/series/77") in calls
    client.close()


def test_rollback_calls_the_opposite_home_assistant_service() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200, json=[{"entity_id": "light.kitchen", "state": "changed"}]
        )

    client = HomeAssistantClient(
        base_url="http://ha.local",
        token="secret",
        transport=httpx.MockTransport(handler),
    )
    siris = _siris()
    siris.tools.register(HomeAssistantCallServiceTool(client, siris.events))

    siris.run_tool(
        "home_assistant_call_service",
        {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"},
        confirmed=True,
    )

    result = RollbackLastActionSkill().run(
        siris, tool_name="home_assistant_call_service"
    )

    assert result["rolled_back"] is True
    assert result["undo_arguments"] == {
        "domain": "light",
        "service": "turn_off",
        "entity_id": "light.kitchen",
    }
    assert "/api/services/light/turn_off" in calls
    client.close()


def test_rollback_does_not_bypass_confirmation_gate_of_the_confirmation_step_itself() -> (
    None
):
    """The rollback skill itself is CONFIRM_REQUIRED at the tool-registry
    level -- calling it through run_tool() without confirmed=True should
    still stop for confirmation, exactly like any other CONFIRM_REQUIRED
    tool. (Skill.run() called directly, as the other tests do, bypasses
    that gate on purpose -- this test proves the gate exists at all.)"""
    siris = _siris()
    with pytest.raises(ConfirmationRequired):
        siris.run_tool("rollback_last_action", {"tool_name": "radarr_add_movie"})


def test_rollback_reports_unsupported_for_an_unknown_tool() -> None:
    siris = _siris()
    result = RollbackLastActionSkill().run(siris, tool_name="docker_restart_container")
    assert result["rolled_back"] is False
    assert "no registered rollback" in result["reason"]


class _FakeAuditWithNoEntries:
    def find_recent(self, tool_name: str, limit: int = 5) -> list:
        return []


class _FakeSirisWithNoAuditHistory:
    """Just enough of SirisAI's surface for this one case -- a real
    SirisAI's audit log is a single shared DB across the whole test
    session, so "no recorded call for radarr_add_movie" can't be proven
    against it (other tests in this very file call that tool too)."""

    def __init__(self) -> None:
        self.audit = _FakeAuditWithNoEntries()


def test_rollback_reports_no_recorded_call_found() -> None:
    result = RollbackLastActionSkill().run(
        _FakeSirisWithNoAuditHistory(), tool_name="radarr_add_movie"
    )
    assert result == {
        "rolled_back": False,
        "reason": "No recorded call to 'radarr_add_movie' found.",
    }


def test_rollback_reports_failure_when_the_original_call_itself_errored() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = RadarrClient(
        base_url="http://radarr.local",
        api_key="key",
        transport=httpx.MockTransport(handler),
    )
    siris = _siris()
    siris.tools.register(RadarrAddMovieTool(client))

    with pytest.raises(httpx.ConnectError):
        siris.run_tool(
            "radarr_add_movie",
            {
                "tmdb_id": 1,
                "title": "Dune",
                "quality_profile_id": 4,
                "root_folder_path": "/movies",
            },
            confirmed=True,
        )

    result = RollbackLastActionSkill().run(siris, tool_name="radarr_add_movie")
    assert result["rolled_back"] is False
    assert "nothing to undo" in result["reason"]
    client.close()
