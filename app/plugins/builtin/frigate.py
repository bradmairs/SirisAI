"""Frigate tools -- SirisAI's first vision integration (app/frigate/client.py):
what a camera actually detected, not just a device's reported state.
Registered only when SIRISAI_FRIGATE_BASE_URL is set -- unlike every
other integration here, SIRISAI_FRIGATE_API_KEY is optional, since a
typical self-hosted Frigate instance has no built-in API auth at all
(see app/frigate/client.py's docstring).

Both tools are READ: this is a first, deliberately narrow pass (camera
list, recent detection events) over Frigate's much larger API -- no
write/control surface exists to gate at a higher tier, since Frigate
itself has nothing to arm/disarm or configure that this codebase reasons
about yet.
"""

from typing import Any

from app.frigate.client import FrigateClient
from app.plugins.base import Tool, ToolPermission
from app.vision.client import VisionClient


class FrigateListCamerasTool(Tool):
    name = "frigate_list_cameras"
    description = "List cameras Frigate is configured to watch, and whether each is currently enabled."
    permission = ToolPermission.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, client: FrigateClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        return self._client.list_cameras()


class FrigateRecentEventsTool(Tool):
    name = "frigate_recent_events"
    description = (
        "List recent Frigate detection events (e.g. a person, car, or package spotted by a "
        "camera), newest first. Optionally filter by camera name (from frigate_list_cameras) "
        "or detected label (e.g. 'person', 'car', 'dog'). Each event includes a snapshot_url/"
        "clip_url when Frigate saved one, for citing what was actually seen."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "camera": {
                "type": "string",
                "description": "Optional camera name to filter to, from frigate_list_cameras.",
            },
            "label": {
                "type": "string",
                "description": "Optional detected label to filter to, e.g. 'person' or 'car'.",
            },
            "limit": {
                "type": "integer",
                "description": "How many recent events to return. Defaults to 10.",
            },
        },
        "required": [],
    }

    def __init__(self, client: FrigateClient) -> None:
        self._client = client

    def run(
        self,
        camera: str | None = None,
        label: str | None = None,
        limit: int = 10,
        **kwargs: Any,
    ) -> Any:
        return self._client.recent_events(camera=camera, label=label, limit=limit)


class FrigateLookAtCameraTool(Tool):
    name = "frigate_look_at_camera"
    description = (
        "Look at what a camera sees right now and describe it, or answer a question about it "
        "(e.g. 'is the garage door open?', 'is there a car in the driveway?', 'who is at the front door?'). "
        "Uses a vision model on the camera's live frame -- for questions no sensor answers."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "camera": {"type": "string", "description": "Camera name, from frigate_list_cameras."},
            "question": {"type": "string", "description": "Optional question to answer about the image."},
        },
        "required": ["camera"],
    }

    def __init__(self, client: FrigateClient, vision: VisionClient) -> None:
        self._client = client
        self._vision = vision

    def run(self, camera: str = "", question: str | None = None, **kwargs: Any) -> Any:
        image = self._client.latest_snapshot(camera)
        return {"camera": camera, "question": question, "description": self._vision.describe([image], question)}


class FrigateDescribeEventTool(Tool):
    name = "frigate_describe_event"
    description = (
        "Describe the snapshot Frigate saved for one detection event (id from frigate_recent_events) -- "
        "what the person/vehicle/animal was actually doing, not just the detected label."
    )
    permission = ToolPermission.READ
    parameters = {
        "type": "object",
        "properties": {
            "event_id": {"type": "string", "description": "The event id, from frigate_recent_events."},
            "question": {"type": "string", "description": "Optional question to answer about the snapshot."},
        },
        "required": ["event_id"],
    }

    def __init__(self, client: FrigateClient, vision: VisionClient) -> None:
        self._client = client
        self._vision = vision

    def run(self, event_id: str = "", question: str | None = None, **kwargs: Any) -> Any:
        image = self._client.event_snapshot(event_id)
        return {"event_id": event_id, "description": self._vision.describe([image], question)}
