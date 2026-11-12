"""Pick which tools to show the model for one turn.

Every tool spec is sent to the model on every turn: with 110 tools that was
~14,200 prompt tokens (vs ~1,050 with none) -- seconds of extra latency on
the home GPU and a model that spends its reasoning listing tools. Most turns
need a handful. This scores each tool against the user's message and offers
the best `max_tools`, always keeping:

- a small core set (memory, clock, gap reporting), and
- any tool already used earlier in this conversation, so a follow-up like
  "now turn it off" still has what it needs.

Scoring: category hints for the things people actually ask about, plus word
overlap between the message and each tool's name and description, weighted so
rare words count more. When an embedding model is configured
(SIRISAI_EMBEDDING_PROVIDER, shared with semantic memory), tools are also
matched by meaning -- "has anything arrived in the post?" finds parcel_list
without sharing a word with it: the tools closest to the message's embedding
get a bonus by rank (rank, not raw similarity, so it behaves the same across
embedding models). Tool embeddings are computed once, in one batch, and
reused; if embedding fails, selection quietly falls back to keywords alone.
A tool the selector wrongly drops can't be called, so the cap is generous and
`max_tools=0` turns selection off entirely. evals/tool_choice.jsonl measures
how often the right tool makes the cut (app/evals/tool_choice.py).
"""

import logging
import math
import re
from collections.abc import Callable, Iterable
from typing import Any

logger = logging.getLogger(__name__)

Embedder = Callable[[list[str]], list[list[float]]]

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and are as at be by can do for from get has have how i if in is it its me my of on or our so that "
    "the their them then there this to up use was we what when where which who will with you your please tell show "
    "undo one no not "
    "give make set run list check".split()
)

# 5.52: web search and fetch are always offered (when they're turned on). Only questions that said "search" or
# "latest" got them before, so "who won the grand final?" was answered from the model's 2023 training instead.
CORE_TOOLS = frozenset(
    {"remember", "recall", "delete_memory", "recall_entity_facts", "remember_entity_fact", "get_current_time", "report_capability_gap",
     "web_search", "web_fetch"}
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
    (frozenset("brain knowledge learned learn note notes vault".split()), ("brain_",)),
    (frozenset("goal goals objective objectives".split()), ("create_goal", "list_goals", "update_goal_status", "add_goal_objective", "complete_goal_objective", "goal_")),
    (frozenset("routine routines every nightly daily weekday weekdays weekend".split()), ("create_routine", "list_routines", "set_routine_enabled")),
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
    # Felt temperature: "it's freezing in here" is a heating request.
    (frozenset("cold freezing chilly warm warmer cooler stuffy heating heat".split()), ("home_assistant_",)),
    (
        frozenset("protocol protocols lockdown secure lock away leaving guest guests visitors visitor mates friends private privacy".split()),
        ("run_protocol", "list_protocols", "create_protocol", "undo_last_protocol", "set_guest_mode", "guest_mode_status", "departure_check"),
    ),
    (frozenset("heading going leaving out".split()), ("departure_check",)),
    (frozenset("email emails mail inbox reply message messages unread".split()), ("email_",)),
    (frozenset("sleep slept rest tired steps walking walked exercise heart fitness health weight".split()), ("health_summary",)),
    (frozenset("readiness flat why changed low recover recovery hrv".split()), ("health_explain", "health_overview")),
    (frozenset("gym lift lifts lifting bench squat deadlift press row workout workouts pr prs record records deload strength sets reps trending".split()), ("gym_progress", "gym_sessions")),
    (frozenset("session sessions workout workouts gym lifted trained training".split()), ("gym_sessions",)),
    (frozenset("car ev charge charging battery range drive driving precondition vehicle".split()), ("car_",)),
    (frozenset("parcel parcels package packages delivery deliveries post courier tracking arrived".split()), ("parcel_",)),
    (frozenset("energy electricity power solar cheap tariff price dishwasher washing dryer".split()), ("energy_status", "schedule_for_cheap_energy", "list_deferred_jobs", "cancel_deferred_job")),
    (frozenset("security secure certificate certificates ssl tls cert vulnerability vulnerabilities cve intrusion hacked snooping".split()), ("security_briefing", "security_scan_images")),
    (frozenset("backup backups".split()), ("backup_check",)),
    (frozenset("habit habits automate automatically routine suggestion suggestions".split()), ("habit_suggestions", "accept_habit_suggestion")),
    (frozenset("yourself own autonomously today fixed".split()), ("what_did_you_do", "remediation_status")),
    (frozenset("voice voices recognise recognize".split()), ("list_voiceprints", "delete_voiceprint")),
    (frozenset("look see outside snooping intruder somebody someone door doorbell front porch driveway yard".split()), ("frigate_look_at_camera", "frigate_recent_events")),
    (frozenset("crash crashing falling down keeps restarting".split()), ("docker_", "remediation_status")),
    (
        frozenset("standard standards clause as3500 as/nzs guideline guidelines authority code engineering civil hydraulic hydraulics "
                  "stormwater sewer sewerage drainage culvert pit pits".split()),
        ("engineering_standards_search", "sirishydro_ask"),
    ),
    (
        frozenset("calculate calculation calculator manning pipe pipes flow capacity grade slope velocity headloss head weir orifice "
                  "detention rational catchment runoff hazen-williams darcy pump buoyancy channel".split()),
        ("engineering_calculate", "engineering_calculators"),
    ),
    (frozenset("project projects job jobs".split()), ("engineering_projects",)),
    (frozenset("sirisos apd reviewer archive gvw timesheet timesheets sirisdrone apps app tile tiles".split()), ("sirisos_apps_status",)),
    (
        frozenset("cpd career chartered chartership cpeng charter registration registered register ner rpe rpev competency competencies "
                  "promotion progression mieaust ea".split()),
        ("career_",),
    ),
]

_HINT_WEIGHT = 4.0
_PER_GROUP = 2
_SEMANTIC_TOP = 12
_SEMANTIC_WEIGHT = 5.0


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


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
    def __init__(self, max_tools: int = 20, embed: Embedder | None = None) -> None:
        self.max_tools = max_tools
        self.embed = embed
        self._doc_cache: dict[str, set[str]] = {}
        self._idf: dict[str, float] = {}
        self._idf_for: int = -1
        self._vectors: dict[str, tuple[str, list[float]]] = {}

    def _semantic_bonus(self, prompt: str, specs: list[dict[str, Any]]) -> dict[str, float]:
        """Rank-based bonus for the tools closest in meaning to `prompt`."""
        if self.embed is None:
            return {}
        try:
            texts = {
                _spec_name(s): f"{_spec_name(s).replace('_', ' ')}: {s['function'].get('description') or ''}" for s in specs
            }
            missing = [n for n, t in texts.items() if self._vectors.get(n, ("", []))[0] != t]
            if missing:
                vectors = self.embed([texts[n] for n in missing])
                for name, vector in zip(missing, vectors, strict=True):
                    self._vectors[name] = (texts[name], vector)
            query = self.embed([prompt])[0]
        except Exception:
            logger.warning("Embedding tools for selection failed; using keywords only", exc_info=True)
            return {}
        ranked = sorted(texts, key=lambda n: _cosine(query, self._vectors[n][1]), reverse=True)[:_SEMANTIC_TOP]
        return {name: _SEMANTIC_WEIGHT * (1 - rank / _SEMANTIC_TOP) for rank, name in enumerate(ranked)}

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
        groups: list[tuple[str, ...]] = []
        for words, prefixes in _HINTS:
            if raw_words & words or prompt_tokens & words:
                hinted.extend(prefixes)
                groups.append(prefixes)

        semantic = self._semantic_bonus(prompt, specs)
        scores: dict[str, float] = {}
        for spec in specs:
            name = _spec_name(spec)
            score = sum(self._idf.get(w, 0.0) for w in prompt_tokens & self._doc_cache.get(name, set()))
            if any(name == h or (h.endswith("_") and name.startswith(h)) for h in hinted):
                score += _HINT_WEIGHT
            scores[name] = score + semantic.get(name, 0.0)

        keep: set[str] = {n for n in CORE_TOOLS} | set(recent_tool_names)
        by_name = {_spec_name(s): s for s in specs}
        keep &= set(by_name)
        # Every family the message points at gets its best couple of tools
        # first: "who's at the front door?" hints both Home Assistant (17
        # tools) and the cameras, and on a tie the bigger family used to
        # fill the whole cap (found by the tool-choice evals).
        for prefixes in groups:
            members = [n for n in scores if any(n == h or (h.endswith("_") and n.startswith(h)) for h in prefixes)]
            for name in sorted(members, key=lambda n: -scores[n])[:_PER_GROUP]:
                if len(keep) < self.max_tools:
                    keep.add(name)
        for name, score in sorted(scores.items(), key=lambda kv: -kv[1]):
            if len(keep) >= self.max_tools:
                break
            if score > 0:
                keep.add(name)
        # Preserve the registry's own order so the prompt stays stable.
        return [s for s in specs if _spec_name(s) in keep]
