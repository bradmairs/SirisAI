"""The hub contract, version 1: what SirisOS (or any other home screen) may
rely on from SirisAI, served under /siris/hub/v1.

Before this, SirisOS ran SirisAI tools by name and read their raw results,
so renaming a tool or reshaping a result quietly emptied part of its daily
brief and nothing in either repo noticed. These models are the contract
instead: every hub response is built as one of them (so SirisAI can't send
something else), `contracts/hub-v1/schema.json` is generated from them (a
test fails when the two drift), and `contracts/hub-v1/examples/` holds one
real-shaped payload per endpoint that SirisOS's own tests parse.

Rules for changing it:
- Adding an optional field is fine within v1: regenerate the schema
  (`python -m app.hub.contract`) and refresh the examples.
- Removing or renaming a field, or changing its type, is a v2.

Response models forbid unknown fields so a composer can't drift from the
schema; request bodies are equally strict.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = "1"
CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts" / "hub-v1"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# -- GET /siris/hub/v1 ---------------------------------------------------------


class HubInfo(_Model):
    contract: Literal["1"] = CONTRACT_VERSION
    app: Literal["SirisAI"] = "SirisAI"
    user: str = Field(description="Who the request is acting as (X-Siris-User, or 'default').")
    guest_mode: bool = Field(description="While on, personal sections are left out and should be hidden by the client too.")
    guest_until: str | None = None
    features: dict[str, bool] = Field(description="Which optional integrations are configured, by name.")
    personal_sections: list[str] = Field(description="Brief/widget sections that are personal (hidden in guest mode).")


# -- GET /siris/hub/v1/brief ---------------------------------------------------


class WeatherNow(_Model):
    temperature_c: float | None = None
    conditions: str | None = None
    humidity_percent: float | None = None
    wind_kmh: float | None = None


class ForecastDay(_Model):
    date: str | None = None
    conditions: str | None = None
    high_c: float | None = None
    low_c: float | None = None
    rain_chance_percent: float | None = None
    precipitation_mm: float | None = None


class Weather(_Model):
    now: WeatherNow | None = None
    today: ForecastDay | None = None


class CalendarEvent(_Model):
    title: str
    start: str | None = Field(None, description="ISO date-time, or a bare date for all-day events.")
    end: str | None = None
    all_day: bool = False
    location: str | None = None
    calendar: str | None = Field(None, description="Which calendar it's on, e.g. Gmail or an iCloud calendar's name.")
    attendees: list[str] = Field([], description="Attendee and organiser email addresses, when the calendar has them.")


class CalendarRange(_Model):
    """Every event from `start` up to (not including) `end`, local dates."""

    start: str = Field(description="First day, YYYY-MM-DD.")
    end: str = Field(description="The day after the last day, YYYY-MM-DD.")
    guest_mode: bool
    events: list[CalendarEvent] | None = Field(None, description="Null in guest mode or when no calendar answered.")
    unavailable: bool = Field(False, description="No calendar is set up, or every calendar failed.")


class Forecast(_Model):
    """The daily forecast at home, today first."""

    days: list[ForecastDay] | None = Field(None, description="Null when no forecast is set up or it failed.")
    unavailable: bool = False


class TodoItem(_Model):
    summary: str
    list: str | None = None
    due: str | None = None


class ImportantEmail(_Model):
    sender: str = Field(description="Display name, falling back to the address.")
    subject: str
    why: list[str] = []


class EmailSummary(_Model):
    unread: int
    important: list[ImportantEmail] = []


class HealthMetric(_Model):
    latest: float | None = None
    day: str | None = None
    week_average: float | None = None
    baseline: float | None = None


class HealthSummary(_Model):
    metrics: dict[str, HealthMetric] = {}
    notes: list[str] = []


class CarSummary(_Model):
    name: str
    summary: str
    battery_percent: float | None = None
    range: float | None = None
    range_unit: str | None = None
    charging: bool | None = None
    plugged_in: bool | None = None
    locked: bool | None = None
    location: str | None = None
    low_battery: bool = False


class ParcelSummary(_Model):
    id: int
    label: str
    status: str | None = None
    status_text: str | None = None
    carrier: str | int | None = None
    eta: str | None = None
    last_event: str | None = None


class EnergySummary(_Model):
    configured: bool
    cheap_now: bool
    reasons: list[str] = []
    solar_watts: float | None = None
    price: float | None = None
    next_cheap_window: str | None = None
    waiting_jobs: int = 0


class OverdueTask(_Model):
    task: str
    note: str | None = None
    days_late: int | None = None


class Deadline(_Model):
    title: str
    days_left: int


class BrainSummary(_Model):
    inbox: int = 0
    overdue_tasks: list[OverdueTask] = []
    deadlines: list[Deadline] = []
    auto_linked: int = 0
    pending_auto_links: int = 0
    unsure_links: int = 0
    highlights: list[str] = []
    topics: list[str] = Field([], description="Tags growing in the vault lately, most active first.")


class AttentionCounts(_Model):
    open: int = 0
    urgent: int = 0


class Brief(_Model):
    generated_at: str
    guest_mode: bool
    hidden: list[str] = Field(description="Sections left out because guest mode is on.")
    weather: Weather | None = None
    calendar: list[CalendarEvent] | None = None
    todo: list[TodoItem] | None = None
    email: EmailSummary | None = None
    health: HealthSummary | None = None
    car: CarSummary | None = None
    parcels: list[ParcelSummary] | None = None
    energy: EnergySummary | None = None
    brain: BrainSummary | None = None
    attention: AttentionCounts
    unavailable: list[str] = Field(description="Configured sections that failed this time (each fails alone).")


# -- GET /siris/hub/v1/widgets -------------------------------------------------


class ProtocolInfo(_Model):
    name: str
    description: str = ""
    builtin: bool = False
    steps: list[str] = []


class LastProtocolRun(_Model):
    protocol: str | None = None
    at: str | None = None
    undone: bool = False
    can_undo: bool = False


class AutonomyItem(_Model):
    time: str
    local_time: str | None = None
    what: str
    why: str | None = None
    tool: str
    outcome: str = "done"
    verified: bool | None = None


class Autonomy(_Model):
    summary: str
    items: list[AutonomyItem] = []


class Widgets(_Model):
    generated_at: str
    guest_mode: bool
    hidden: list[str]
    car: CarSummary | None = None
    parcels: list[ParcelSummary] | None = None
    energy: EnergySummary | None = None
    protocols: list[ProtocolInfo] = []
    last_protocol: LastProtocolRun | None = None
    cameras: list[str] = []
    autonomy: Autonomy | None = None
    attention: AttentionCounts
    unavailable: list[str]


# -- GET /siris/hub/v1/search --------------------------------------------------


class SearchHit(_Model):
    kind: Literal["note", "chat", "memory"]
    title: str
    snippet: str = ""
    ref: str = Field(description="Note title, conversation id or memory id: what the client deep-links with.")
    when: str | None = None
    note_type: str | None = None


class SearchResults(_Model):
    query: str
    hits: list[SearchHit]
    unavailable: list[str] = []


# -- Attention feed (/siris/hub/v1/attention) ------------------------------------


class AttentionAction(_Model):
    id: str = Field(description="Pass to POST .../attention/{item}/act.")
    label: str
    style: Literal["primary", "secondary", "danger"] = "secondary"
    confirm: str | None = Field(None, description="If set, ask the user this before acting.")


# -- the day: presence, reminders, email with people, upkeep, health by day -------


class Presence(_Model):
    home: bool | None = Field(description="Whether the last presence check found someone home; null when presence isn't set up.")
    checked_at: str | None = None
    since: str | None = Field(default=None, description="When that state began (an arrival or a departure); null until "
                              "there's been a change since tracking started.")


class Reminder(_Model):
    id: int
    message: str
    due_at: str = Field(description="ISO date-time, UTC.")
    status: Literal["pending", "sent"]


class ReminderList(_Model):
    guest_mode: bool
    reminders: list[Reminder] | None = Field(None, description="Pending and recently sent reminders; null in guest mode.")


class ReminderDone(_Model):
    id: int
    status: str


class EmailBrief(_Model):
    uid: str
    sender: str
    subject: str
    date: str | None = None
    snippet: str = ""


class EmailWith(_Model):
    guest_mode: bool
    configured: bool
    messages: list[EmailBrief] | None = Field(None, description="Newest first; null in guest mode or when email isn't set up.")


class BackupStatus(_Model):
    newest: str | None = Field(None, description="When the newest backup was made, ISO.")
    count: int = 0
    stale: bool = False
    last_drill: str | None = None
    drill_ok: bool | None = None


class UpdateStatus(_Model):
    current: str
    latest: str | None = None
    available: bool = False
    checked_at: str | None = None


class Upkeep(_Model):
    backups: BackupStatus | None = None
    update: UpdateStatus


class HealthDay(_Model):
    date: str
    metrics: dict[str, float] = {}


class HealthDays(_Model):
    guest_mode: bool
    days: list[HealthDay] | None = Field(None, description="Oldest first; null in guest mode.")


class HealthOverview(_Model):
    """Everything the Health section shows (SirisAI 3.51; SirisOS 5.8 reads it
    instead of keeping its own store). The sections are open objects so they
    can grow without a v2; see app/health/overview.py and scores.py."""

    guest_mode: bool
    today: str | None = None
    available: bool = False
    sources: dict[str, Any] = {}
    readiness: dict[str, Any] | None = Field(None, description="score 0-100, band (Primed/Steady/Recover), advice, early, contributors, summary; locked (kept as it was this morning, 3.62) and locked_at")
    energy: dict[str, Any] | None = Field(None, description="Today's energy (3.62): now and start (0-100), wake, timeline [{time, value}], spent {awake, activity, stress}, regained, summary, estimated")
    sleep: dict[str, Any] | None = Field(None, description="need, last_night (hours, stages, start, end), score, debt_7d, bedtime_spread_min, average_7d, nights")
    activity: dict[str, Any] | None = Field(None, description="steps, energy, exercise, load (ratio, zone), series, workouts")
    vitals: list[dict[str, Any]] | None = None
    weight: dict[str, Any] | None = Field(None, description="latest, trend (smoothed), per_week, goal_kg, to_go_kg, weeks_to_goal, series")
    getting_sick: dict[str, Any] | None = Field(None, description="Set when temperature, resting heart rate and HRV point to illness: level 'likely' (two days running) or 'early' (today, 3.82)")
    trends: list[dict[str, Any]] | None = Field(None, description="This week against your last 30 days (3.82): key, label, unit, week, baseline, change_pct, direction (up/down/steady), good")
    logged: dict[str, Any] | None = Field(None, description="Today's water, caffeine, drinks, mood, and the week's notes (symptoms, supplements, meals)")
    insights: list[dict[str, Any]] | None = Field(None, description="Findings like 'after a late meeting you sleep 40 min less'")
    bedtime: dict[str, Any] | None = Field(None, description="Tonight's suggested bedtime and why")


class HealthWorkouts(_Model):
    """Workouts, newest first (SirisAI 3.51): for SirisOS's training plan
    (what was actually run, race predictions, heart-rate zones)."""

    guest_mode: bool
    workouts: list[dict[str, Any]] | None = Field(None, description="date, name, start, minutes, km, kcal, avg_hr, max_hr, source")


class AttentionItem(_Model):
    id: int
    key: str = Field(description="Stable identity of the underlying thing (a suggestion, a job, an alert).")
    kind: Literal["alert", "confirm", "suggestion", "review", "waiting", "notice"]
    severity: Literal["urgent", "attention", "info"]
    title: str
    body: str = ""
    source: str = Field(description="What raised it, e.g. 'intruder_check', 'routines', 'habits'.")
    link: str | None = Field(None, description="A hint where to look, e.g. 'brain:links' or 'cameras:front_door'.")
    actions: list[AttentionAction] = []
    status: Literal["open", "done", "dismissed"]
    outcome: str | None = None
    created_at: str
    updated_at: str


class AttentionList(_Model):
    items: list[AttentionItem]
    counts: AttentionCounts


class AttentionActRequest(_Model):
    action: str
    body: str | None = Field(None, max_length=20000, description="For a triage reply's 'send': the reply as edited, instead of the draft. For a new device's 'name': the name to give it.")
    pin: str | None = Field(
        None,
        max_length=32,
        description="The approval PIN, when SirisAI has one and this approves a high-risk action. Without it the reply is 403 with detail starting 'pin_required'.",
    )


class AttentionActResult(_Model):
    item: AttentionItem
    result: Any = None


class AttentionEvent(_Model):
    """One message on GET /siris/hub/v1/attention/stream (an SSE `data:` line).
    `snapshot` comes first with every open item; then `upsert` / `remove`."""

    type: Literal["snapshot", "upsert", "remove"]
    items: list[AttentionItem] = []
    counts: AttentionCounts


# -- Protocols and cameras -------------------------------------------------------


class ProtocolStep(_Model):
    label: str
    tool: str
    would_run: bool
    reason: str | None = None


class ProtocolPreview(_Model):
    name: str
    description: str = ""
    steps: list[ProtocolStep]


class ProtocolRunRequest(_Model):
    confirmed: bool = Field(False, description="Protocols change locks, covers and modes: the user must confirm first.")


class ProtocolRunResult(_Model):
    name: str
    ran: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    can_undo: bool = False


class ProtocolStepDef(_Model):
    tool: str = Field(description="home_assistant_call_service, notify or set_guest_mode (others only as made in chat).")
    arguments: dict[str, Any] = {}
    label: str | None = None
    when: Any = Field(None, description="nobody_home, someone_home, night, day, {entity, state|not_state}, or null for always.")
    undo: dict[str, Any] | None = Field(None, description="A step's own undo (set in chat; kept as it is).")


class ProtocolDef(_Model):
    name: str
    description: str = ""
    builtin: bool = Field(False, description="A built-in as it comes; false once it's yours (made here or in chat).")
    customised: bool = Field(False, description="Your version of a built-in: deleting it brings the original back.")
    steps: list[ProtocolStepDef]


class ProtocolEntity(_Model):
    entity_id: str
    name: str
    domain: str
    device_class: str | None = None


class ProtocolCatalogue(_Model):
    protocols: list[ProtocolDef]
    actions: dict[str, list[str]] = Field(description="Home Assistant actions a step can take, per domain.")
    entities: list[ProtocolEntity] = []
    home_assistant: bool


class ProtocolSave(_Model):
    description: str = ""
    steps: list[ProtocolStepDef] = Field(min_length=1, max_length=30)


class ProtocolDeleted(_Model):
    name: str
    deleted: bool
    restored_builtin: bool = False


class CameraLookRequest(_Model):
    question: str | None = None


class CameraLook(_Model):
    camera: str
    description: str
    question: str | None = None


# -- POST /siris/hub/v1/llm/complete ---------------------------------------------


class LLMCompleteRequest(_Model):
    task: str = Field(min_length=1, max_length=64, description="Who is asking and why, e.g. 'sirishydro'. Logged.")
    system: str = Field("", max_length=8000)
    prompt: str = Field(min_length=1, max_length=48000)
    role: Literal["general", "fast"] | None = None


class LLMCompleteResult(_Model):
    text: str
    role: str
    model: str | None = None


# -- Home controls (/siris/hub/v1/controls, SirisOS 4.2) ---------------------------


class Control(_Model):
    entity_id: str
    name: str
    kind: Literal["light", "switch", "garage", "lock"]
    state: str = Field(description="Home Assistant's state: on/off, open/closed/opening/closing, locked/unlocked...")
    brightness_pct: int | None = None
    confirm: str | None = Field(None, description="Set when the action needs the user's yes first (garage doors and locks).")


class SceneControl(_Model):
    id: str = Field(description="siris:<n> for a Siris scene, or a Home Assistant scene.* entity id.")
    name: str
    source: Literal["siris", "home_assistant"]


class Controls(_Model):
    configured: bool
    guest_mode: bool
    controls: list[Control] = []
    scenes: list[SceneControl] = []


class ControlRequest(_Model):
    action: Literal["toggle", "turn_on", "turn_off", "open", "close", "lock", "unlock"]
    confirmed: bool = Field(False, description="Garage doors and locks: the user said yes.")


class ControlResult(_Model):
    entity_id: str
    action: str
    state: str | None = None


class NetworkBlockRequest(_Model):
    blocked: bool = Field(description="True blocks the client; false lets it back in.")


class NetworkBlockResult(_Model):
    id: str = Field(description="The client's MAC address.")
    blocked: bool


# -- Power use (/siris/hub/v1/energy/usage, SirisOS 5.3's house view) -----------


class PowerSensor(_Model):
    entity_id: str
    name: str
    watts: float = Field(description="Power right now, in watts (kW sensors are converted).")


class EnergyMeter(_Model):
    entity_id: str
    name: str
    kwh: float = Field(description="The meter's reading, a total that only goes up (kWh; Wh converted).")


class EnergyUsage(_Model):
    configured: bool = Field(description="Home Assistant is set up.")
    house_watts: float | None = Field(None, description="The whole house now: SIRISAI_ENERGY_GRID_ENTITY_ID, else none.")
    solar_watts: float | None = Field(None, description="SIRISAI_ENERGY_SOLAR_ENTITY_ID right now.")
    house_meter: str | None = Field(None, description="SIRISAI_ENERGY_METER_ENTITY_ID: the whole house's kWh meter, if set.")
    power: list[PowerSensor] = []
    meters: list[EnergyMeter] = []


# -- The network, live (/siris/hub/v1/network, SirisOS 4.5's house view) -----------


class NetworkNode(_Model):
    id: str = Field(description="The MAC address.")
    name: str
    kind: Literal["gateway", "switch", "access_point", "client"]
    model: str | None = None
    wired: bool | None = None
    online: bool = True
    uplink: str | None = Field(None, description="The id of the switch or access point it connects through (null: the gateway).")
    rx_bps: float = Field(0, description="Bytes per second it's receiving now.")
    tx_bps: float = Field(0, description="Bytes per second it's sending now.")


class Network(_Model):
    configured: bool
    guest_mode: bool
    checked_at: str
    wan: dict[str, float | str | None] | None = Field(None, description="The internet link: rx_bps, tx_bps, isp.")
    nodes: list[NetworkNode] = []


ENDPOINTS: dict[str, type[BaseModel]] = {
    "info": HubInfo,
    "brief": Brief,
    "widgets": Widgets,
    "search": SearchResults,
    "calendar": CalendarRange,
    "forecast": Forecast,
    "attention": AttentionList,
    "attention_act_request": AttentionActRequest,
    "attention_act": AttentionActResult,
    "attention_event": AttentionEvent,
    "protocol_preview": ProtocolPreview,
    "protocol_run_request": ProtocolRunRequest,
    "protocol_run": ProtocolRunResult,
    "protocols": ProtocolCatalogue,
    "protocol_save": ProtocolSave,
    "protocol_saved": ProtocolDef,
    "protocol_deleted": ProtocolDeleted,
    "camera_look_request": CameraLookRequest,
    "camera_look": CameraLook,
    "llm_complete_request": LLMCompleteRequest,
    "llm_complete": LLMCompleteResult,
    "presence": Presence,
    "reminders": ReminderList,
    "reminder_done": ReminderDone,
    "email_with": EmailWith,
    "upkeep": Upkeep,
    "health_days": HealthDays,
    "health_overview": HealthOverview,
    "health_workouts": HealthWorkouts,
    "controls": Controls,
    "control_request": ControlRequest,
    "control": ControlResult,
    "network": Network,
    "network_block_request": NetworkBlockRequest,
    "network_block": NetworkBlockResult,
    "energy_usage": EnergyUsage,
}


def schema() -> dict[str, Any]:
    """One JSON Schema document with a definition per endpoint model."""
    defs: dict[str, Any] = {}
    roots: dict[str, Any] = {}
    for name, model in ENDPOINTS.items():
        doc = model.model_json_schema(ref_template="#/$defs/{model}")
        defs.update(doc.pop("$defs", {}))
        defs[model.__name__] = doc
        roots[name] = {"$ref": f"#/$defs/{model.__name__}"}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "SirisAI hub contract",
        "version": CONTRACT_VERSION,
        "endpoints": roots,
        "$defs": dict(sorted(defs.items())),
    }


def schema_text() -> str:
    return json.dumps(schema(), indent=2, sort_keys=False) + "\n"


if __name__ == "__main__":
    # python -m app.hub.contract            -> rewrite contracts/hub-v1/schema.json
    # python -m app.hub.contract --check    -> exit 1 if it's out of date
    path = CONTRACT_DIR / "schema.json"
    if "--check" in sys.argv:
        sys.exit(0 if path.exists() and path.read_text() == schema_text() else 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(schema_text())
    print(f"wrote {path}")
