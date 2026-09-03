from unittest.mock import patch

from app.core.config import Settings
from app.core.siris import SirisAI


def test_system_info_always_registered() -> None:
    siris = SirisAI(settings=Settings())
    assert siris.tools.get("system_info") is not None


def test_optional_tool_libraries_not_registered_by_default() -> None:
    siris = SirisAI(settings=Settings())
    names = {tool.name for tool in siris.tools.list()}
    assert not names & {
        "docker_list_containers",
        "docker_restart_container",
        "plex_now_playing",
        "plex_library_sections",
        "unifi_list_devices",
        "unifi_list_clients",
        "weather_current",
        "calendar_upcoming_events",
        "hermes_run_task",
    }


def test_docker_tools_registered_when_configured() -> None:
    siris = SirisAI(settings=Settings(docker_base_url="unix:///var/run/docker.sock"))
    try:
        names = {tool.name for tool in siris.tools.list()}
        assert {"docker_list_containers", "docker_restart_container"} <= names
    finally:
        assert siris._docker_client is not None
        siris._docker_client.close()


def test_plex_tools_registered_when_both_settings_present() -> None:
    siris = SirisAI(settings=Settings(plex_base_url="http://plex.local:32400", plex_token="secret"))
    try:
        names = {tool.name for tool in siris.tools.list()}
        assert {"plex_now_playing", "plex_library_sections"} <= names
    finally:
        assert siris._plex_client is not None
        siris._plex_client.close()


def test_unifi_tools_registered_when_both_settings_present() -> None:
    siris = SirisAI(settings=Settings(unifi_base_url="https://unifi.local", unifi_api_key="secret"))
    try:
        names = {tool.name for tool in siris.tools.list()}
        assert {"unifi_list_devices", "unifi_list_clients"} <= names
    finally:
        assert siris._unifi_client is not None
        siris._unifi_client.close()


def test_weather_tool_registered_when_both_coordinates_present() -> None:
    siris = SirisAI(settings=Settings(weather_latitude=51.5, weather_longitude=-0.1))
    try:
        assert siris.tools.get("weather_current") is not None
    finally:
        assert siris._weather_client is not None
        siris._weather_client.close()


def test_weather_tool_not_registered_with_only_one_coordinate() -> None:
    siris = SirisAI(settings=Settings(weather_latitude=51.5))
    assert siris.tools.get("weather_current") is None


def test_calendar_tool_registered_when_url_present() -> None:
    with patch("app.calendar.client.caldav.DAVClient"):
        siris = SirisAI(settings=Settings(caldav_url="https://cal.example.com"))
    assert siris.tools.get("calendar_upcoming_events") is not None


def test_hermes_tool_registered_when_base_url_present() -> None:
    siris = SirisAI(settings=Settings(hermes_base_url="http://localhost:8420"))
    try:
        assert siris.tools.get("hermes_run_task") is not None
    finally:
        assert siris._hermes_client is not None
        siris._hermes_client.close()
