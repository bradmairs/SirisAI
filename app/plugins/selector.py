"""Pick which tools to show the model for one turn.

Every tool spec is sent to the model on every turn: with 110 tools that was
~14,200 prompt tokens (vs ~1,050 with none) -- seconds of extra latency on
the home GPU and a model that spends its reasoning listing tools. Most turns
need a handful. This scores each tool against the user's message and offers
the best `max_tools`, always keeping:

- a small core set (memory, clock, gap reporting), and
- any tool already used earlier in this conversation, so a follow-up like
  "now turn it off" still has what it needs.

Scoring is deliberately simple and deterministic (no model call): category
hints for the things people actually ask about, plus word overlap between the
message and each tool's name and description, weighted so rare words count
more. A tool the selector wrongly drops can't be called, so the cap is
generous and `max_tools=0` turns selection off entirely.
"""

import math
import re
from collections.abc import Iterable
from typing import Any

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and are as at be by can do for from get has have how i if in is it its me my of on or our so that "
    "the their them then there this to up use was we what when where which who will with you your please tell show "
    "undo one no not "
    "give make set run list check".split()
)

CORE_TOOLS = frozenset(
    {"remember", "recall", "delete_memory", "recall_entity_facts", "remember_entity_fact", "get_current_time", "report_capability_gap"}
)

# words in the user's message -> tool-name prefixes / exact names to favour
_HINTS: list[tuple[frozenset[str], tuple[str, ...]]] = [
    (
        frozenset(
            "light lights lamp lamps bulb bulbs switch switches thermostat climate fan heater blind blinds cover lock unlock "
            "door garage automation automations sensor sensors logbook history template on off dim brightness colour color "
            "temperature humidity homeassistant assistant home lounge office kitchen bedroom bathroom hallway study "
            "speaker speakers media play pause volume music".split()
        ),
        ("home_assistant_",),
    ),
    (
        frozenset("movie movies film films tv series show shows episode episodes season radarr sonarr download downloads downloading queue "
                  "subtitle subtitles bazarr plex watch watched watching tautulli library libraries stream streaming audiobook".split()),
        ("plex_", "tautulli_", "radarr_", "sonarr_", "bazarr_", "cleanuparr_", "listenarr_"),
    ),
    (
        frozenset("network wifi wi-fi unifi client clients device devices router gateway internet speed bandwidth ping latency online offline reachable endpoint".split()),
        ("unifi_", "run_speed_test", "check_service_endpoint", "new_device_alert"),
    ),
    (frozenset("docker container containers image restart logs".split()), ("docker_",)),
    (
        frozenset("server cpu memory ram disk storage health uptime load resources hermes terminal command maintenance".split()),
        ("system_info", "server_health_check", "self_maintenance_check", "hermes_", "rebuild_and_restart"),
    ),
    (frozenset("note notes obsidian vault".split()), ("obsidian_",)),
    (frozenset("goal goals objective objectives".split()), ("create_goal", "list_goals", "update_goal_status", "add_goal_objective", "complete_goal_objective", "goal_")),
    (frozenset("routine routines".split()), ("create_routine", "list_routines", "set_routine_enabled")),
    (
        frozenset("remind reminder reminders timer timers alarm minutes hours notify notification push alert".split()),
        ("set_reminder", "list_reminders", "cancel_reminder", "create_geofence_reminder", "list_geofence_reminders", "set_geofence_reminder_enabled", "notify", "list_pending_notifications"),
    ),
    (frozenset("cron crontab scheduled schedule jobs".split()), ("cron_",)),
    (frozenset("weather forecast rain raining umbrella sunny cold hot wind".split()), ("weather_",)),
    (frozenset("calendar meeting meetings appointment event events agenda today tomorrow".split()), ("calendar_", "meeting_prep", "home_assistant_get_calendar_events")),
    (frozenset("todo todos to-do shopping groceries list tasks task".split()), ("home_assistant_get_todo_items", "home_assistant_add_todo_item", "home_assistant_complete_todo_item")),
    (frozenset("search web google news latest lookup online website article".split()), ("web_search", "web_fetch")),
    (frozenset("forget remember memory memories know saved earlier previously conversation conversations discussed talked".split()), ("search_conversations", "recent_conversations")),
    (frozenset("why explain undo rollback trust audit activity did".split()), ("explain_recent_action", "rollback_last_action", "trust_ledger")),
    (
        frozenset("briefing morning goodnight bed sleep night evening day house everything".split()),
        ("daily_briefing", "bedtime_check", "house_status_check", "server_health_check"),
    ),
    (frozenset("camera cameras frigate".split()), ("frigate_",)),
    (frozenset("synology nas backup backups".split()), ("synology_",)),
    (frozenset("gap capability capabilities missing".split()), ("list_capability_gaps", "dismiss_capability_gap", "resolve_capability_gap")),
    (frozenset("event events happened recently log".split()), ("log_event", "recall_events", "delete_event", "event_")),
    (frozenset("relationship relationships depends depend affected runs uses".split()), ("remember_entity_relationship", "recall_entity_relationships")),
    (frozenset("who home present presence away".split()), ("home_assistant_list_entities", "home_assistant_get_state", "presence_")),
    (frozenset("integration integrations working broken health status".split()), ("integration_health",)),
]

_HINT_WEIGHT = 4.0


def _tokens(text: str) -> list[str]:
    out = []
    for word in _WORD_RE.findall(text.lower()):
        if word in _STOPWORDS or len(word) < 3:
            continue
        # crude plural stemming: "lights" -> "light"
        out.append(word[:-1] if word.endswith("s") and len(word) > 3 else word)
    return out


def _spec_name(spec: dict[str, Any]) -> str:
    return spec["function"]["name"]


class ToolSelector:
    def __init__(self, max_tools: int = 32) -> None:
        self.max_tools = max_tools
        self._doc_cache: dict[str, set[str]] = {}
        self._idf: dict[str, float] = {}
        self._idf_for: int = -1

    def _prepare(self, specs: list[dict[str, Any]]) -> None:
        if self._idf_for == len(specs):
            return
        self._doc_cache = {}
        df: dict[str, int] = {}
        for spec in specs:
            fn = spec["function"]
            words = set(_tokens(fn["name"].replace("_", " ") + " " + (fn.get("description") or "")))
            self._doc_cache[fn["name"]] = words
            for w in words:
                df[w] = df.get(w, 0) + 1
        n = max(len(specs), 1)
        self._idf = {w: math.log(1 + n / c) for w, c in df.items()}
        self._idf_for = len(specs)

    def select(
        self,
        prompt: str,
        specs: list[dict[str, Any]],
        recent_tool_names: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        if self.max_tools <= 0 or len(specs) <= self.max_tools:
            return specs
        self._prepare(specs)

        prompt_tokens = set(_tokens(prompt))
        raw_words = set(_WORD_RE.findall(prompt.lower()))
        hinted: list[str] = []
        for words, prefixes in _HINTS:
            if raw_words & words or prompt_tokens & words:
                hinted.extend(prefixes)

        scores: dict[str, float] = {}
        for spec in specs:
            name = _spec_name(spec)
            score = sum(self._idf.get(w, 0.0) for w in prompt_tokens & self._doc_cache.get(name, set()))
            if any(name == h or (h.endswith("_") and name.startswith(h)) for h in hinted):
                score += _HINT_WEIGHT
            scores[name] = score

        keep: set[str] = {n for n in CORE_TOOLS} | set(recent_tool_names)
        by_name = {_spec_name(s): s for s in specs}
        keep &= set(by_name)
        for name, score in sorted(scores.items(), key=lambda kv: -kv[1]):
            if len(keep) >= self.max_tools:
                break
            if score > 0:
                keep.add(name)
        # Preserve the registry's own order so the prompt stays stable.
        return [s for s in specs if _spec_name(s) in keep]
