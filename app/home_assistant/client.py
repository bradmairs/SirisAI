"""Thin REST client for Home Assistant
(https://developers.home-assistant.io/docs/api/rest/).

Synchronous on purpose: `Tool.run()` is a synchronous interface (see
app/plugins/base.py) and these are cheap, local-network requests, so there's
no need to bridge async/sync just for this.

`create_automation()` (milestone 54) and `delete_automation()` (a
hardening pass) are the two methods here that aren't on the documented
REST API page above -- there's no public endpoint to persist or remove
an automation. Both use Home Assistant's config-editor API
(`/api/config/automation/config/<id>`, the same endpoint the frontend's
own automation editor calls), which takes the same bearer-token auth as
everything else here and is widely used by community tooling, but isn't
part of the officially documented REST surface -- confirm it against a
real instance before relying on it (see the README's milestone 54
writeup for exactly what is and isn't verified). An automation's id
(needed by delete_automation) is exposed as the `id` attribute on its
entity's state -- `get_state("automation.some_slug")` already surfaces
it, no separate lookup method needed.
"""

import time
from datetime import UTC, datetime, timedelta
from typing import Any

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

    def create_automation(
        self,
        alias: str,
        trigger: list[dict[str, Any]],
        action: list[dict[str, Any]],
        condition: list[dict[str, Any]] | None = None,
        mode: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Creates a new automation and returns Home Assistant's response
        with `automation_id` merged in (not something HA's own response
        includes -- there's no other way to hand the caller back which id
        got used).

        `trigger`/`condition`/`action` are passed through as-is rather
        than validated against Home Assistant's own trigger/condition/
        action schema -- that schema is exactly the kind of thing an LLM
        already knows well from training data (it's extensively
        documented, public YAML), and re-encoding it here as a rigid tool
        schema risks this codebase's own copy drifting from Home
        Assistant's, or missing a trigger/condition/action type this
        codebase's author never thought to include. Home Assistant's own
        config validation is still the real backstop either way -- a
        malformed payload gets rejected by HA, not silently accepted.

        The automation's id is a fresh millisecond timestamp, the same
        convention Home Assistant's own UI uses when you create a new
        automation there -- colliding with an existing one (and silently
        overwriting it) is realistically not going to happen."""
        automation_id = str(int(time.time() * 1000))
        config: dict[str, Any] = {"alias": alias, "trigger": trigger, "action": action}
        if condition:
            config["condition"] = condition
        if mode:
            config["mode"] = mode
        if description:
            config["description"] = description
        response = self._client.post(f"/api/config/automation/config/{automation_id}", json=config)
        response.raise_for_status()
        result = response.json()
        result["automation_id"] = automation_id
        return result

    def get_history(self, entity_id: str, hours: int = 24) -> list[dict]:
        """State-over-time for one entity -- e.g. "how has the temperature
        changed today" -- distinct from get_logbook, which is about
        discrete named events and their cause, not a raw value series."""
        since = datetime.now(UTC) - timedelta(hours=hours)
        response = self._client.get(
            f"/api/history/period/{since.isoformat()}", params={"filter_entity_id": entity_id}
        )
        response.raise_for_status()
        series = response.json()
        entries = series[0] if series else []
        return [{"state": e.get("state"), "last_changed": e.get("last_changed")} for e in entries]

    def render_template(self, template: str) -> str:
        """Evaluates a Jinja2 template against Home Assistant's current
        state (https://www.home-assistant.io/docs/configuration/templating/)
        -- read-only: rendering computes a value, it doesn't act on
        anything. Lets a caller ask for something no single existing tool
        returns directly, e.g. an average across several sensors, using
        Home Assistant's own templating instead of this codebase
        reinventing a query language. Unlike every other call here, the
        response is plain rendered text, not JSON."""
        response = self._client.post("/api/template", json={"template": template})
        response.raise_for_status()
        return response.text

    def delete_automation(self, automation_id: str) -> dict[str, Any]:
        response = self._client.delete(f"/api/config/automation/config/{automation_id}")
        response.raise_for_status()
        return {"deleted": True, "automation_id": automation_id}

    def close(self) -> None:
        self._client.close()
