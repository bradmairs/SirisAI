"""ORM models. Import this module wherever `Base.metadata.create_all` runs
so tables get registered -- `app/core/siris.py` pulls it in transitively via
`app.memory.service`."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MemoryEntry(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(Text)  # "user" | "assistant" | "tool"
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_arguments: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON-encoded
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))


class ToolExecution(Base):
    """One SirisAI.run_tool() call, successful or not."""

    __tablename__ = "tool_executions"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"), nullable=True, index=True)
    tool_name: Mapped[str] = mapped_column(Text)
    arguments: Mapped[str] = mapped_column(Text)  # JSON-encoded
    result: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON-encoded; unset if error is set
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))
