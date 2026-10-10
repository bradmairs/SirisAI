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


ENDPOINTS: dict[str, type[BaseModel]] = {
    "info": HubInfo,
    "brief": Brief,
    "widgets": Widgets,
    "search": SearchResults,
    "attention": AttentionList,
    "attention_act_request": AttentionActRequest,
    "attention_act": AttentionActResult,
    "attention_event": AttentionEvent,
    "protocol_preview": ProtocolPreview,
    "protocol_run_request": ProtocolRunRequest,
    "protocol_run": ProtocolRunResult,
    "camera_look_request": CameraLookRequest,
    "camera_look": CameraLook,
    "llm_complete_request": LLMCompleteRequest,
    "llm_complete": LLMCompleteResult,
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
