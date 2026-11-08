import json

from app.core.config import Settings
from app.core.siris import SirisAI
from app.plugins.selector import CORE_TOOLS, ToolSelector


def _spec(name: str, description: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": {}}}}


def _library() -> list[dict]:
    specs = [_spec(n, f"{n.replace('_', ' ')} core memory tool") for n in sorted(CORE_TOOLS)]
    specs += [
        _spec("home_assistant_call_service", "Call a Home Assistant service to control a light, switch or device."),
        _spec("home_assistant_resolve_entity", "Find Home Assistant entities matching a loose name like office light."),
        _spec("home_assistant_get_state", "Get the state of one Home Assistant entity."),
        _spec("plex_now_playing", "List what is playing on Plex right now."),
        _spec("run_speed_test", "Measure internet download and upload speed and ping."),
        _spec("radarr_queue", "List the Radarr movie download queue."),
        _spec("sonarr_calendar", "Upcoming TV episodes from Sonarr."),
        _spec("unifi_list_clients", "List clients connected to the UniFi network."),
        _spec("docker_list_containers", "List Docker containers and whether they are running."),
        _spec("weather_current", "Current weather conditions."),
        _spec("obsidian_search", "Search notes in the Obsidian vault."),
    ]
    specs += [_spec(f"filler_tool_{i}", f"unrelated filler capability number {i}") for i in range(60)]
    return specs


def _names(specs: list[dict]) -> list[str]:
    return [s["function"]["name"] for s in specs]


def test_selection_is_off_when_max_is_zero_or_everything_already_fits() -> None:
    specs = _library()
    assert ToolSelector(0).select("turn on the light", specs) == specs
    assert ToolSelector(500).select("turn on the light", specs) == specs


def test_a_device_command_gets_the_home_assistant_tools() -> None:
    picked = _names(ToolSelector(20).select("Turn on the office light at 40% brightness", _library()))
    assert {"home_assistant_call_service", "home_assistant_resolve_entity"} <= set(picked)
    assert "plex_now_playing" not in picked and len(picked) <= 20


def test_service_questions_find_their_own_tools() -> None:
    sel = ToolSelector(20)
    assert "plex_now_playing" in _names(sel.select("Is anything playing on Plex right now?", _library()))
    assert "run_speed_test" in _names(sel.select("How fast is my internet?", _library()))
    assert "radarr_queue" in _names(sel.select("What is in my Radarr download queue?", _library()))
    assert "unifi_list_clients" in _names(sel.select("How many devices are on the UniFi network?", _library()))
    assert "docker_list_containers" in _names(sel.select("Which docker containers are running?", _library()))
    assert "weather_current" in _names(sel.select("Should I take an umbrella? What's the weather?", _library()))


def test_core_memory_tools_are_always_offered() -> None:
    picked = set(_names(ToolSelector(20).select("Tell me about plex", _library())))
    assert CORE_TOOLS <= picked


def test_tools_used_earlier_in_the_conversation_stay_available_for_follow_ups() -> None:
    picked = _names(ToolSelector(20).select("Now turn it off.", _library(), recent_tool_names=["plex_now_playing"]))
    assert "plex_now_playing" in picked


def test_cap_is_respected_and_registry_order_is_kept() -> None:
    specs = _library()
    picked = ToolSelector(15).select("turn on the light and check plex and the weather", specs)
    assert len(picked) <= 15 + len(CORE_TOOLS)
    order = {n: i for i, n in enumerate(_names(specs))}
    positions = [order[n] for n in _names(picked)]
    assert positions == sorted(positions)


def test_selection_shrinks_the_prompt_substantially() -> None:
    specs = _library()
    picked = ToolSelector(20).select("turn on the office light", specs)
    assert len(json.dumps(picked)) < len(json.dumps(specs)) / 2


def test_small_talk_gets_only_the_core_set() -> None:
    picked = set(_names(ToolSelector(20).select("hi there", _library())))
    assert picked <= CORE_TOOLS | {"echo"}


def test_available_tool_specs_uses_the_selector_only_when_given_a_prompt() -> None:
    siris = SirisAI(settings=Settings(tool_selection_max=5))
    everything = siris.available_tool_specs()
    narrowed = siris.available_tool_specs("hi there")
    assert len(narrowed) < len(everything)
    assert siris.available_tool_specs(None) == everything



def test_web_search_is_always_offered_for_current_questions() -> None:
    # 5.52: "who won the grand final" shares no word with the web tools, but needs them.
    names = ["web_search", "web_fetch", "home_assistant_turn_on", "calendar_list_events", "weather_forecast"]
    specs = [_spec(n, f"{n.replace('_', ' ')} tool") for n in names]
    picked = {s["function"]["name"] for s in ToolSelector(max_tools=4).select("who won the 2026 AFL grand final?", specs)}
    assert {"web_search", "web_fetch"} <= picked
