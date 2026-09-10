"""Thin client for Frigate's REST API
(https://docs.frigate.video/integrations/api/) -- a self-hosted NVR with
local object detection. This is SirisAI's first real vision integration:
everything else in this codebase reasons over text/state from other
services, never over what a camera actually saw.

Built from Frigate's own published API docs (well-documented, unlike
Cleanuparr/Listenarr's guessed surfaces -- closer to UniFi's tier: a real
spec, just no live camera hardware here to confirm the exact response
shape against). Deliberately read-only and deliberately narrow: two
endpoints (camera list, recent detection events) rather than Frigate's
full surface (recordings, live MSE/WebRTC streams, review items, model
config) -- those need a real instance to confirm are worth building
against before guessing at their shape, the same restraint this
codebase's other least-verified integrations already use.

No arm/disarm concept here on purpose -- Frigate itself has none (it
always detects); "should this trigger a notification/automation" is a
Home Assistant/NotificationManager-side decision, not something to fake
up in this client.

Unlike every other header-auth integration here, `api_key` is optional:
Frigate has no built-in API authentication in a typical self-hosted setup
(its own docs recommend a reverse-proxy in front of it for that) --
sent as a bearer token when given, for the setups that do put one there,
but SirisAI works against an unauthenticated instance too, so this is
gated on `base_url` alone (app/core/siris.py), not on both being set like
every api-key-required integration.
"""

from datetime import UTC, datetime
from typing import Any

import httpx


class FrigateClient:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.Client(
            base_url=self._base_url, timeout=timeout, headers=headers, transport=transport
        )

    def list_cameras(self) -> list[dict]:
        """Configured cameras, from Frigate's own config -- not a
        detection query, so this works even for a camera with zero
        recent activity."""
        response = self._client.get("/api/config")
        response.raise_for_status()
        cameras = response.json().get("cameras", {})
        return [
            {"name": name, "enabled": cfg.get("enabled", True)}
            for name, cfg in cameras.items()
        ]

    def recent_events(
        self, camera: str | None = None, label: str | None = None, limit: int = 10
    ) -> list[dict]:
        """Recent detection events (a person, car, package, ...), newest
        first -- Frigate's own default ordering. `snapshot_url`/
        `clip_url` are built here rather than left as the raw booleans
        Frigate returns (`has_snapshot`/`has_clip`), so a caller gets a
        link straight to the image/video instead of having to know
        Frigate's URL shape itself; None when that particular event has
        neither."""
        params: dict[str, Any] = {"limit": limit}
        if camera:
            params["camera"] = camera
        if label:
            params["label"] = label
        response = self._client.get("/api/events", params=params)
        response.raise_for_status()
        return [
            {
                "id": event.get("id"),
                "camera": event.get("camera"),
                "label": event.get("label"),
                "zones": event.get("zones", []),
                "top_score": event.get("top_score"),
                "start_time": _to_iso(event.get("start_time")),
                "end_time": _to_iso(event.get("end_time")),
                "snapshot_url": (
                    f"{self._base_url}/api/events/{event['id']}/snapshot.jpg"
                    if event.get("has_snapshot")
                    else None
                ),
                "clip_url": (
                    f"{self._base_url}/api/events/{event['id']}/clip.mp4"
                    if event.get("has_clip")
                    else None
                ),
            }
            for event in response.json()
        ]

    def close(self) -> None:
        self._client.close()


def _to_iso(unix_timestamp: float | None) -> str | None:
    """Frigate reports event times as Unix timestamps (seconds); every
    other timestamp this codebase returns to a caller is ISO 8601, so
    this converts rather than leaking Frigate's own raw number format."""
    if unix_timestamp is None:
        return None
    return datetime.fromtimestamp(unix_timestamp, tz=UTC).isoformat()
