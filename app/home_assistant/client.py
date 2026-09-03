"""Thin REST client for Home Assistant
(https://developers.home-assistant.io/docs/api/rest/).

Synchronous on purpose: `Tool.run()` is a synchronous interface (see
app/plugins/base.py) and these are cheap, local-network requests, so there's
no need to bridge async/sync just for this.
"""

from datetime import datetime

import httpx


class HomeAssistantClient:
    def __init__(self, base_url: str, token: str, timeout: float = 10.0, transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
        )

    def list_states(self, domain: str | None = None) -> list[dict]:
        response = self._client.get("/api/states")
        response.raise_for_status()
        states = response.json()
        if domain:
            states = [s for s in states if s["entity_id"].startswith(f"{domain}.")]
        return states

    def get_state(self, entity_id: str) -> dict:
        response = self._client.get(f"/api/states/{entity_id}")
        response.raise_for_status()
        return response.json()

    def call_service(self, domain: str, service: str, entity_id: str | None = None, **service_data: object) -> object:
        payload = dict(service_data)
        if entity_id:
            payload["entity_id"] = entity_id
        response = self._client.post(f"/api/services/{domain}/{service}", json=payload)
        response.raise_for_status()
        return response.json()

    def get_logbook(self, entity_id: str | None = None, since: datetime | None = None) -> list[dict]:
        """Human-readable "what happened and why" -- state changes AND
        the automation/context that caused them, where Home Assistant
        knows one -- not just current state (`get_state`). `since` omitted
        defaults, per Home Assistant's own API, to the last day."""
        path = "/api/logbook"
        if since is not None:
            path = f"{path}/{since.isoformat()}"
        response = self._client.get(path, params={"entity": entity_id} if entity_id else None)
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self._client.close()
