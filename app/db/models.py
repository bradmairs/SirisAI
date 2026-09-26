"""ORM models. Import this module wherever `Base.metadata.create_all` runs
so tables get registered -- `app/core/siris.py` pulls it in transitively via
`app.memory.service`."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MemoryEntry(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    # Multi-user recognition (milestone 46) -- see app/core/user_context.py.
    # Defaults to the single "default" user, so an unconfigured deployment
    # (no X-Siris-User header ever sent) behaves exactly as before this
    # column existed.
    user_id: Mapped[str] = mapped_column(Text, default="default", index=True)
    # Indexed (a performance hardening pass): MemoryStore.recall()/
    # list_all() order by this -- without an index, that's a full-table
    # scan + sort on every call, unnoticeable on a fresh install and a
    # real cost once months of memories have accumulated. See
    # app/db/migrate.py for how an existing pre-this-milestone SQLite
    # file picks the index up too, since create_all() only creates it
    # for a brand-new table.
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC), index=True
    )
    # Usage tracking (milestone 30) -- bumped every time this entry is
    # returned by MemoryStore.recall(), so the self_maintenance_check
    # skill can flag memories that have genuinely never come back up
    # ("unused"), not just old ones.
    recall_count: Mapped[int] = mapped_column(Integer, default=0)
    last_recalled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Semantic recall (app/llm/embeddings.py) -- JSON-encoded list[float],
    # null for any row saved before SIRISAI_EMBEDDING_PROVIDER was
    # configured (or if that call failed). MemoryStore.recall() computes
    # and backfills this lazily the first time such a row is a candidate,
    # rather than requiring a separate migration script to run.
    embedding: Mapped[str | None] = mapped_column(Text, nullable=True)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id"), index=True
    )
    role: Mapped[str] = mapped_column(Text)  # "user" | "assistant" | "tool"
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_arguments: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # JSON-encoded
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )


class ToolExecution(Base):
    """One SirisAI.run_tool() call, successful or not."""

    __tablename__ = "tool_executions"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversations.id"), nullable=True, index=True
    )
    tool_name: Mapped[str] = mapped_column(Text)
    arguments: Mapped[str] = mapped_column(Text)  # JSON-encoded
    result: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # JSON-encoded; unset if error is set
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # True when this row is a CONFIRM_REQUIRED tool's pending proposal --
    # the tool did NOT actually run (see app/plugins/base.py's
    # ConfirmationRequired). A later confirmed run of the same tool gets
    # its own separate row.
    confirmation_required: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[float] = mapped_column(Float)
    # Explainable autonomy (milestone 28) -- all three nullable since most
    # calls before this milestone (and plenty since, e.g. a plain READ
    # lookup) have nothing meaningful to say here. trigger: what caused
    # this call ("chat: model chose this tool", "planner: step 2 of an
    # in-progress plan", "proactive check: automated schedule", "direct
    # API request"). evidence: JSON-encoded supporting data that justified
    # it, when there was any (e.g. a planner's prior step results, or the
    # health-check warnings that led to a notify call). policy: what
    # allowed it to run without asking first, derived from the tool's own
    # ToolPermission by SirisAI.run_tool() -- not something callers
    # invent themselves.
    trigger: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON-encoded
    policy: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Deterministic per-tool verification (a hardening pass; see
    # Tool.verify()) -- null for every tool that doesn't define one (most
    # of them), same "nullable, most rows have nothing to say here"
    # reasoning as trigger/evidence/policy above. verified: whether the
    # independent check passed. verification_detail: why.
    verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    verification_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Indexed (a performance hardening pass) -- this is the highest-write-
    # volume table in the schema (every SirisAI.run_tool() call adds a
    # row) and AuditLogStore.list_recent()/find_recent() both order by
    # this on every read (GET /siris/activity, explain_recent_action);
    # see MemoryEntry.created_at's comment above for the full reasoning.
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC), index=True
    )


class EntityFact(Base):
    """One fact about a named entity -- a person, service, device, project,
    etc. Several rows can share an entity_name; together they're everything
    SirisAI knows about that entity. Distinct from the free-text
    `memories` table: this is for information clearly about one named
    thing, looked up per-entity rather than by substring search."""

    __tablename__ = "entity_facts"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_name: Mapped[str] = mapped_column(Text, index=True)
    fact: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )


class EntityRelationship(Base):
    """One directed, typed edge between two entities -- the world model
    (milestone 21): `subject_entity RELATION object_entity`, e.g.
    ("Plex", "RUNS_ON", "main-server") or ("Plex", "DEPENDS_ON",
    "media-storage"). `relation` is free text (e.g. "RUNS_ON", "USES",
    "DEPENDS_ON") rather than an enum -- same reasoning as EntityFact's
    free-text `fact`: the model chooses the wording, SirisAI just stores
    and queries it. Distinct from EntityFact: a fact is a standalone
    statement about one entity, a relationship connects two."""

    __tablename__ = "entity_relationships"

    id: Mapped[int] = mapped_column(primary_key=True)
    subject_entity: Mapped[str] = mapped_column(Text, index=True)
    relation: Mapped[str] = mapped_column(Text)
    object_entity: Mapped[str] = mapped_column(Text, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )


class Event(Base):
    """A point-in-time occurrence -- a status change, failure, completion,
    etc -- optionally tagged to an entity. Distinct from an EntityFact
    (a standing fact) and from ToolExecution (an internal record of what
    SirisAI itself did): an Event is a thing that happened, which may or
    may not have come from a tool call.

    `source` and `data` are populated for events pushed in through the
    event bus (POST /siris/events, see app/api/routes/events.py) --
    which system it came from and any extra structured payload; both are
    None for events the model logs itself via the log_event tool."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_name: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    description: Mapped[str] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    data: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON-encoded
    # Indexed (a performance hardening pass) -- EventMemoryStore.recent_events()
    # orders by this on every call; see MemoryEntry.created_at's comment above.
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC), index=True
    )


class PendingNotification(Base):
    """A notification-worthy item (milestone 72, an "interruption
    controller") that's deliberately NOT been pushed immediately -- only
    NotificationManager's non-urgent "notify" tier ever lands here;
    "urgent" still pushes right away via the `notify` tool, same as
    before this existed. Queued here instead, to be folded into the next
    daily_briefing rather than interrupting the user for something that
    can wait for the digest. See app/notify/pending.py."""

    __tablename__ = "pending_notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    message: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC), index=True
    )


class Goal(Base):
    """Something the user wants worked toward that survives past one
    conversation (milestone 23), e.g. "reduce homelab power consumption"
    -- so it doesn't need re-explaining every session. `status` is free
    text (recommended: "active"/"completed"/"abandoned") rather than an
    enum, same reasoning as EntityFact's free-text `fact` and
    EntityRelationship's free-text `relation` elsewhere in this codebase.
    Deliberately just persistent state -- nothing here works a goal
    autonomously in the background; that's the planner (24)."""

    __tablename__ = "goals"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active")
    # Multi-user recognition (milestone 46) -- see MemoryEntry.user_id
    # above and app/core/user_context.py; same default, same reasoning.
    user_id: Mapped[str] = mapped_column(Text, default="default", index=True)
    # Indexed (a performance hardening pass) -- GoalStore.list_goals()
    # orders by this on every call; see MemoryEntry.created_at's comment
    # above. updated_at (stale_goals()'s own order-by column) isn't --
    # that query already filters to status="active" first, a small
    # subset in practice, so the extra index wouldn't earn its upkeep
    # cost the way created_at's does for the unfiltered full list.
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )


class GoalObjective(Base):
    """One step toward a Goal. Several rows share a goal_id -- together
    they're that goal's objective list, each independently markable done."""

    __tablename__ = "goal_objectives"

    id: Mapped[int] = mapped_column(primary_key=True)
    goal_id: Mapped[int] = mapped_column(ForeignKey("goals.id"), index=True)
    description: Mapped[str] = mapped_column(Text)
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )


class MetricSample(Base):
    """One point-in-time reading of a named metric (milestone 27), e.g.
    metric_name="cpu_percent", value=6.2. Accumulates only when something
    actually calls MetricsStore.record_sample() -- there's no in-process
    scheduler taking readings on its own (milestone 19's boundary) -- so
    this naturally piggybacks on whatever already calls
    server_health_check. Several rows share a metric_name; together they're
    the recent history MetricsStore.baseline() learns a per-server-normal
    range from, instead of ServerHealthSkill's old one-size-fits-all fixed
    threshold."""

    __tablename__ = "metric_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    metric_name: Mapped[str] = mapped_column(Text, index=True)
    value: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )


class Routine(Base):
    """A named, structured routine (milestone 29) -- an ordered list of
    tool names to run together, e.g. "every night, check my server and
    tell me tomorrow if anything went wrong" becomes
    name="overnight_server_watch", interval_minutes=1440,
    actions=["server_health_check"]. Execution is deliberately NOT
    scheduled by SirisAI itself -- milestone 19 kept scheduling external
    (cron/systemd), and this milestone's design question (asked of, and
    answered by, the user rather than picked silently) kept that boundary:
    `interval_minutes`/`last_run_at` let SirisAI answer "is this routine
    due" when asked, but something external has to ask, via
    POST /siris/routines/run_due, the same relationship
    POST /siris/proactive/check already has to server_health_check.

    `interval_minutes` is deliberately simple -- "due at least every N
    minutes since it last ran" -- not a full cron expression. Covers
    "nightly" (1440) and "every 15 minutes" (15) fine; does NOT support
    "nightly at 2am specifically" or any other time-of-day-bound
    schedule -- an honest v1 scope cut, not a hidden limitation."""

    __tablename__ = "routines"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    trigger_description: Mapped[str] = mapped_column(Text)  # free text, e.g. "nightly"
    interval_minutes: Mapped[int] = mapped_column(Integer)
    actions: Mapped[str] = mapped_column(
        Text
    )  # JSON-encoded list[str] of tool names, run in order
    output_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )


class CapabilityGap(Base):
    """A recorded "SirisAI can't do this yet" (milestone 31) -- what the
    model itself reported it couldn't fulfill with any currently
    registered tool (see `/siris/capabilities`), plus whatever it could
    propose from its own general knowledge: a plausible real-world
    integration and a sketch of a tool that would provide it. Detection
    is deliberately NOT a separate mechanism here -- the model already
    sees the full tool list on every chat turn, so recognizing a gap is
    just the model's own reasoning; this table exists so that recognition
    turns into a persisted, queryable development spec instead of
    evaporating at the end of one chat reply."""

    __tablename__ = "capability_gaps"

    id: Mapped[int] = mapped_column(primary_key=True)
    requested_capability: Mapped[str] = mapped_column(Text)
    suggested_integration: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_tool_spec: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # JSON-encoded
    # "open" (default) | "resolved" | "failed" | "dismissed" -- a gap's
    # lifecycle (a Jarvis-concept follow-up): "open" is the only state
    # report_capability_gap ever creates; the other three are set by
    # dismiss_capability_gap or resolve_capability_gap
    # (app/plugins/builtin/capability_gap.py) once someone -- a human or
    # Hermes Agent -- has actually acted on it. Plain str, not an Enum
    # column: every status check elsewhere in this codebase (Goal.status,
    # Routine.enabled) already compares against plain values, not a DB
    # enum type.
    status: Mapped[str] = mapped_column(Text, default="open")
    # Free text explaining a "resolved"/"failed" status -- Hermes Agent's
    # own report of what it did (or why it couldn't), verbatim. None
    # while a gap is still "open" or after a plain dismiss with no
    # explanation attached.
    resolution_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )


class SatelliteRegistration(Base):
    """A voice satellite's room + how to reach it (milestone 48) -- one row
    per room; re-registering the same room (a restarted satellite
    process, a changed port) overwrites callback_url/last_active_at
    rather than accumulating duplicates. last_active_at is bumped by
    both registration/heartbeat and real voice activity
    (SatelliteStore.record_activity()) -- current_room() is just
    "whichever room has the highest last_active_at", so either kind of
    activity can make a room current."""

    __tablename__ = "satellite_registrations"

    id: Mapped[int] = mapped_column(primary_key=True)
    room: Mapped[str] = mapped_column(Text, unique=True, index=True)
    callback_url: Mapped[str] = mapped_column(Text)
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )
    # Conversation handoff (milestone 75): the conversation_id last in
    # play from this room, so a fresh request from a *different* room
    # with no conversation_id of its own can pick up where the last one
    # left off -- see SatelliteStore.handoff_candidate().
    last_conversation_id: Mapped[str | None] = mapped_column(Text, nullable=True)


class Integration(Base):
    """A discovered integration (milestone 32) -- everything needed to
    regenerate its tools on the next startup, not just the tools
    themselves (which live only in the in-memory ToolRegistry). Stores
    the raw spec rather than just the operations parsed out of it, so a
    future smarter parser can re-derive more from the same stored data
    without re-fetching anything. auth_header_value is a credential --
    treat it like the ntfy topic elsewhere in this codebase: never
    returned by IntegrationStore.list_integrations()'s default (public)
    path, only read back internally when SirisAI re-registers tools at
    startup."""

    __tablename__ = "integrations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(
        Text
    )  # prefixes every tool this integration generates
    base_url: Mapped[str] = mapped_column(Text)
    spec: Mapped[str] = mapped_column(Text)  # JSON-encoded OpenAPI spec
    auth_header_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_header_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )


class KnownNetworkDevice(Base):
    """A Home Assistant device_tracker entity_id NewDeviceAlertSkill (a
    hardening pass) has already seen -- one row per device, so a later
    check can tell "already known" from "just appeared" without keeping
    that set anywhere in memory (this process restarts; SQLite doesn't).
    Nothing beyond entity_id/first_seen_at is stored -- no friendly name,
    no MAC -- since all this needs to answer is set membership; anything
    richer belongs to Home Assistant's own device registry, not a copy of
    it here."""

    __tablename__ = "known_network_devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )


class EventReactionCursor(Base):
    """Single-row cursor: the highest `Event.id` EventReactionSkill (a
    hardening pass) has already classified, so composing it into
    `POST /siris/proactive/check` on a schedule doesn't re-flag the same
    old event on every run -- same "persist across restarts" reasoning
    `KnownNetworkDevice` uses for new_device_alert, just a watermark
    instead of a set since events only ever accumulate by increasing id.
    Exactly one row ever exists, at id=1."""

    __tablename__ = "event_reaction_cursor"

    id: Mapped[int] = mapped_column(primary_key=True)
    last_event_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class GeofenceReminder(Base):
    """A one-shot or recurring reminder triggered by a Home Assistant
    `person`/`device_tracker` entity's `state` becoming a specific zone
    (e.g. entity_id='person.brad', zone='home', message='pick up the
    mail') -- generalizes PresenceSkill's own household-wide UniFi
    home/away trigger (milestone 42) to an arbitrary entity and zone,
    same "trigger condition -> action" concept, just not hardcoded to
    exactly that one pair.

    `last_known_zone` is GeofenceReminderCheckSkill's own baseline for
    detecting a transition -- same "only act on a change, not every
    tick" reasoning PresenceSkill's own event-based baseline uses, just
    stored on the row itself since there's one reminder per row rather
    than one shared household state. `repeat=False` (the default) is a
    genuine reminder: it disables itself (`enabled=False`) the first
    time it fires, mirroring how a real one-off reminder shouldn't keep
    firing every time you come home forever; `repeat=True` is a standing
    rule instead ("let me know whenever Brad leaves work"), left enabled
    to keep firing on every future transition into the zone.

    Scoped per user (see GoalStore's own docstring for the same
    reasoning) -- this is personal ("remind me"), not shared
    infrastructure like a Routine."""

    __tablename__ = "geofence_reminders"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[str] = mapped_column(Text)
    zone: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    repeat: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_known_zone: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_id: Mapped[str] = mapped_column(Text, default="default", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )


class Scene(Base):
    """A named, ordered bundle of Home Assistant service calls the user
    or model can trigger as one unit -- e.g. name="movie_night",
    steps=[{"domain": "light", "service": "turn_off", "entity_id":
    "light.living_room"}, {"domain": "media_player", "service":
    "turn_on", "entity_id": "media_player.tv"}] -- a Jarvis-concept
    brainstorm item: "first-class scenes, not raw HA automation YAML."

    Deliberately just an ordered list of
    home_assistant_call_service-shaped steps (same `domain`/`service`/
    `entity_id`/`service_data` shape that tool already takes), not a
    trigger condition -- that's what an HA automation is for (see
    HomeAssistantCreateAutomationTool). A scene runs once, on demand,
    when asked, the same "no in-process scheduler" boundary this
    codebase draws everywhere else (milestone 19); running one is
    RunSceneSkill's job (app/skills/builtin/run_scene.py), not this
    model's.

    Not user-scoped, same reasoning as Routine: this is shared home
    infrastructure ("movie night" isn't personal to one user of a
    household), not personal state like a Goal or GeofenceReminder."""

    __tablename__ = "scenes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    steps: Mapped[str] = mapped_column(Text)  # JSON-encoded list[dict]
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )


class Reminder(Base):
    """A time-based reminder or timer: "remind me at 7pm", "timer for 10
    minutes". Delivered as a push notification by the background scheduler
    (app/core/scheduler.py) at `due_at` (naive UTC, like every other
    timestamp here). `status` is pending -> sent, or cancelled / failed."""

    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(primary_key=True)
    message: Mapped[str] = mapped_column(Text)
    due_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(Text, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
