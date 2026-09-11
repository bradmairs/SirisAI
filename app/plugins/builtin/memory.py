"""Memory tools -- how the LLM reads and writes SirisAI's memory during a
conversation. Four categories, each with its own storage
(app/memory/service.py, app/memory/entities.py, app/memory/events.py) and
its own pair of tools below:

- Free-text long-term memory (`remember`/`recall`) -- general facts and
  preferences, searched by embedding similarity when SIRISAI_EMBEDDING_PROVIDER
  is configured (plain substring match otherwise -- see app/memory/service.py).
  The original, still the right choice for anything that isn't clearly about
  one named thing.
- Entity memory (`remember_entity_fact`/`recall_entity_facts`) -- facts
  about a specific named person/service/device/project, looked up by that
  name rather than searched.
- Event memory (`log_event`/`recall_events`) -- things that happened,
  optionally tagged to an entity, so "has Plex been playing up lately?"
  can be answered from history instead of just current state.
- Entity relationships (`remember_entity_relationship`/
  `recall_entity_relationships`) -- the world model (milestone 21): typed
  edges between entities (e.g. Plex RUNS_ON main-server), so SirisAI can
  answer "what depends on X" instead of only "what do you know about X".

`delete_memory` (user-requested) is a deliberate reversal of
`MemoryStore.delete()`'s original "not exposed as a model tool"
decision -- deciding what to forget was intentionally withheld from the
LLM at first, but a user can ask SirisAI to forget something directly
("forget that I mentioned X"), and the Memory tab (app/web/static/index.html)
needed a delete action of its own regardless. SAFE_ACTION, same tier as
`delete_scene`: a memory (like a scene) is low-stakes to lose --
deleting it doesn't touch any device -- even though, unlike a scene,
there's no undo once it's gone.
"""

from typing import Any

from app.memory.entities import EntityMemoryStore
from app.memory.events import EventMemoryStore
from app.memory.service import MemoryStore
from app.plugins.base import Tool, ToolPermission


class RememberTool(Tool):
    name = "remember"
    description = (
        "Save a general piece of information to long-term memory for later recall -- "
        "facts about the user, their hobbies, preferences, etc. If the information is "
        "clearly about one specific named person/service/device, prefer "
        "remember_entity_fact instead so it can be looked up by that name later."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The information to remember."},
        },
        "required": ["content"],
    }

    def __init__(self, memory: MemoryStore) -> None:
        self._memory = memory

    def run(self, content: str = "", **kwargs: Any) -> Any:
        return self._memory.remember(content)


class RecallTool(Tool):
    name = "recall"
    description = (
        "Search long-term memory for previously saved information about the "
        "user. Omit the query to get the most recently saved memories."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Optional search text."},
        },
        "required": [],
    }

    def __init__(self, memory: MemoryStore) -> None:
        self._memory = memory

    def run(self, query: str | None = None, **kwargs: Any) -> Any:
        return self._memory.recall(query=query)


class DeleteMemoryTool(Tool):
    name = "delete_memory"
    description = (
        "Permanently delete a saved memory, e.g. when asked to forget something. Use the "
        "memory's id field from recall (not its position -- position shifts as memories are "
        "added or deleted, id doesn't). There is no undo."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "memory_id": {"type": "integer", "description": "The memory's id, from recall."},
        },
        "required": ["memory_id"],
    }

    def __init__(self, memory: MemoryStore) -> None:
        self._memory = memory

    def run(self, memory_id: int = 0, **kwargs: Any) -> Any:
        deleted = self._memory.delete(memory_id)
        if not deleted:
            raise ValueError(f"Unknown memory id: {memory_id}")
        return {"deleted": True, "id": memory_id}


class RememberEntityFactTool(Tool):
    name = "remember_entity_fact"
    description = (
        "Save a fact about a specific named entity -- a person, service, device, or "
        "project (e.g. entity_name='Plex', fact='runs on the media server, port 32400'). "
        "Multiple facts can be saved about the same entity over time; use "
        "recall_entity_facts to get all of them back."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "entity_name": {"type": "string", "description": "The entity this fact is about, e.g. 'Plex'."},
            "fact": {"type": "string", "description": "The fact to remember."},
        },
        "required": ["entity_name", "fact"],
    }

    def __init__(self, entities: EntityMemoryStore) -> None:
        self._entities = entities

    def run(self, entity_name: str = "", fact: str = "", **kwargs: Any) -> Any:
        return self._entities.remember_fact(entity_name, fact)


class RecallEntityFactsTool(Tool):
    name = "recall_entity_facts"
    description = "Get everything remembered about a specific named entity (person, service, device, project, etc)."
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "entity_name": {"type": "string", "description": "The entity to recall facts about, e.g. 'Plex'."},
        },
        "required": ["entity_name"],
    }

    def __init__(self, entities: EntityMemoryStore) -> None:
        self._entities = entities

    def run(self, entity_name: str = "", **kwargs: Any) -> Any:
        return self._entities.facts_about(entity_name)


class LogEventTool(Tool):
    name = "log_event"
    description = (
        "Record that something happened -- a status change, failure, completion, etc -- "
        "optionally tagged to an entity (e.g. entity_name='Plex', description='container "
        "crashed and restarted'). Use this for a point-in-time occurrence, not a standing "
        "fact -- use remember_entity_fact for those."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "What happened."},
            "entity_name": {"type": "string", "description": "Optional entity this event relates to."},
        },
        "required": ["description"],
    }

    def __init__(self, events: EventMemoryStore) -> None:
        self._events = events

    def run(self, description: str = "", entity_name: str | None = None, **kwargs: Any) -> Any:
        return self._events.log_event(description, entity_name=entity_name)


class RecallEventsTool(Tool):
    name = "recall_events"
    description = (
        "Get recent events, optionally filtered to one entity -- e.g. for 'has Plex been "
        "playing up lately?', call with entity_name='Plex' to see its recent history."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "entity_name": {"type": "string", "description": "Optional entity to filter events to."},
            "limit": {"type": "integer", "description": "Max events to return. Defaults to 20."},
        },
        "required": [],
    }

    def __init__(self, events: EventMemoryStore) -> None:
        self._events = events

    def run(self, entity_name: str | None = None, limit: int = 20, **kwargs: Any) -> Any:
        return self._events.recent_events(entity_name=entity_name, limit=limit)


class RememberEntityRelationshipTool(Tool):
    name = "remember_entity_relationship"
    description = (
        "Record a typed relationship between two entities, e.g. "
        "(subject_entity='Plex', relation='RUNS_ON', object_entity='main-server') or "
        "(subject_entity='Plex', relation='DEPENDS_ON', object_entity='media-storage'). "
        "relation is free text you choose -- common ones are RUNS_ON, USES, DEPENDS_ON, "
        "EXPOSED_THROUGH -- but use whatever best describes the connection. This builds up "
        "a world model SirisAI can later query with recall_entity_relationships, e.g. to "
        "answer 'what would shutting down X affect'."
    )
    permission = ToolPermission.SAFE_ACTION
    parameters = {
        "type": "object",
        "properties": {
            "subject_entity": {"type": "string", "description": "The entity the relationship starts from."},
            "relation": {"type": "string", "description": "How subject_entity relates to object_entity, e.g. 'RUNS_ON'."},
            "object_entity": {"type": "string", "description": "The entity the relationship points to."},
        },
        "required": ["subject_entity", "relation", "object_entity"],
    }

    def __init__(self, entities: EntityMemoryStore) -> None:
        self._entities = entities

    def run(self, subject_entity: str = "", relation: str = "", object_entity: str = "", **kwargs: Any) -> Any:
        return self._entities.add_relationship(subject_entity, relation, object_entity)


class RecallEntityRelationshipsTool(Tool):
    name = "recall_entity_relationships"
    description = (
        "Get every known relationship touching a named entity, split into 'outgoing' "
        "(this entity is the subject, e.g. what it RUNS_ON or USES) and 'incoming' (this "
        "entity is the object -- what points at it, e.g. what DEPENDS_ON it). Use the "
        "incoming list to answer 'what would shutting down X affect'."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "entity_name": {"type": "string", "description": "The entity to look up relationships for, e.g. 'Plex'."},
        },
        "required": ["entity_name"],
    }

    def __init__(self, entities: EntityMemoryStore) -> None:
        self._entities = entities

    def run(self, entity_name: str = "", **kwargs: Any) -> Any:
        return self._entities.relationships_for(entity_name)
