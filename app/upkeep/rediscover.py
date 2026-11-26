"""Find a service that moved (4.29.0).

Two live breakages this week were the same story: nothing was wrong with
the service, only with where SirisAI looked for it. Hermes became
unreachable when SirisAI's Docker network moved to a new subnet, and
Obsidian stopped answering when its container was recreated and its relay
didn't start again. Both were fixed by hand.

Every 15 minutes, for each integration that doesn't answer, this looks
where a person would:

- the service's container (matched by name or image) at its current
  address on each Docker network it's on, on its own ports;
- the server's LAN address (SIRISAI_SERVER_LAN_HOST, and the hosts of the
  integrations that do answer) on the container's published ports;
- the container's name, for Docker's own DNS.

The first address that answers goes in the feed: "Hermes seems to have
moved: use http://192.168.1.20:8642?" Use it saves the address (as a
Settings-page override, so .env is untouched) and switches SirisAI to it
straight away. A stopped companion container -- SIRISAI_INTEGRATION_COMPANIONS
("Obsidian=obsidian-relay"), or a stopped container named after the service
-- is offered as a restart to approve. Nothing changes without a tap.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.hub.attention import action, raise_confirmation, register_action

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)
SOURCE = "rediscover"
MAX_PROBES = 16
_PRIVATE = re.compile(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)")

# integration name -> (setting holding its address, words its container is called by)
FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "Ollama": ("ollama_base_url", ("ollama",)),
    "Home Assistant": ("home_assistant_base_url", ("homeassistant", "home-assistant", "hass")),
    "Plex": ("plex_base_url", ("plex",)),
    "Tautulli": ("tautulli_base_url", ("tautulli",)),
    "Radarr": ("radarr_base_url", ("radarr",)),
    "Sonarr": ("sonarr_base_url", ("sonarr",)),
    "Bazarr": ("bazarr_base_url", ("bazarr",)),
    "Obsidian": ("obsidian_base_url", ("obsidian",)),
    "Hermes": ("hermes_base_url", ("hermes",)),
    "Frigate": ("frigate_base_url", ("frigate",)),
    "Listenarr": ("listenarr_base_url", ("listenarr",)),
    "Cleanuparr": ("cleanuparr_base_url", ("cleanuparr",)),
}


def _answers(url: str, timeout: float = 2.0) -> bool:
    try:
        httpx.get(url, timeout=timeout, verify=False, follow_redirects=False)
        return True
    except Exception:
        return False


def _with(url: str, host: str, port: int | None) -> str:
    parts = urlsplit(url)
    netloc = f"{host}:{port}" if port else host
    return urlunsplit((parts.scheme or "http", netloc, parts.path, parts.query, ""))


def _matching(containers: list[dict[str, Any]], words: tuple[str, ...]) -> list[dict[str, Any]]:
    return [c for c in containers if any(w in c["name"].lower() or w in c["image"].lower() for w in words)]


def candidates(url: str, words: tuple[str, ...], containers: list[dict[str, Any]], lan_hosts: list[str]) -> list[str]:
    old = urlsplit(url)
    old_port = old.port
    out: list[str] = []
    found = _matching(containers, words)
    running = [c for c in found if c.get("state") == "running"]
    for c in running:
        ports = [p for p in [old_port, *c["ports"]] if p]
        for ip in c["ips"]:
            out += [_with(url, ip, p) for p in ports]
        out += [_with(url, c["name"], p) for p in ports]
    for host in lan_hosts:
        ports = [p for p in [old_port, *(p for c in running for p in c["published"])] if p]
        out += [_with(url, host, p) for p in ports]
    current = url.rstrip("/")
    return [u for u in dict.fromkeys(out) if u.rstrip("/") != current][:MAX_PROBES]


def _lan_hosts(siris: "SirisAI", down: set[str]) -> list[str]:
    from app.core.health import _integration_targets

    hosts = [h.strip() for h in (siris.settings.server_lan_host or "").split(",") if h.strip()]
    for name, url in _integration_targets(siris.settings):
        host = urlsplit(url).hostname or ""
        if name not in down and _PRIVATE.match(host):
            hosts.append(host)
    return list(dict.fromkeys(hosts))


def _companions(siris: "SirisAI") -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for part in (siris.settings.integration_companions or "").split(";"):
        name, sep, containers = part.partition("=")
        if sep:
            out[name.strip().lower()] = [c.strip() for c in containers.split(",") if c.strip()]
    return out


def check(siris: "SirisAI") -> dict[str, Any]:
    from app.core.health import check_integrations

    results = check_integrations(siris.settings)
    down = {r["name"] for r in results if not r["ok"] and r["name"] in FIELDS}
    if not down:
        return {"down": [], "moved": [], "restarts": []}
    docker = getattr(siris, "_docker_client", None)
    try:
        containers = docker.addresses() if docker is not None else []
    except Exception:
        logger.info("Couldn't list containers for rediscovery", exc_info=True)
        containers = []
    hosts = _lan_hosts(siris, down)
    companions = _companions(siris)
    moved, restarts = [], []
    for name in sorted(down):
        field, words = FIELDS[name]
        url = getattr(siris.settings, field, "") or ""
        found = next((c for c in candidates(url, words, containers, hosts) if _answers(c)), None)
        if found:
            siris.attention.raise_item(
                f"{SOURCE}:{field}:{found}", "suggestion", f"{name} seems to have moved",
                source=SOURCE, severity="attention",
                body=f"{name} doesn't answer at {url} any more, but it does at {found}. Use the new address?",
                actions=[action("use", "Use it", "primary"), action("dismiss", "Not now")],
                payload={"name": name, "field": field, "old": url, "new": found}, reopen=False,
            )
            moved.append({"name": name, "old": url, "new": found})
            continue
        stopped = [c["name"] for c in containers if c.get("state") != "running" and (
            c["name"] in companions.get(name.lower(), []) or c in _matching(containers, words))]
        for container in stopped[:2]:
            raise_confirmation(siris, f"rediscover: {name} isn't answering and {container} is stopped", "docker_restart_container", {"container": container})
            restarts.append(container)
    return {"down": sorted(down), "moved": moved, "restarts": restarts}


def switch(siris: "SirisAI", old: str, new: str) -> int:
    """Point the clients already made at `old` to `new`, so the change works without a restart."""
    changed = 0
    seen: set[int] = set()

    def visit(obj: Any, depth: int = 0) -> None:
        nonlocal changed
        if obj is None or id(obj) in seen or depth > 3:
            return
        seen.add(id(obj))
        for attr in ("_client", "client"):
            client = getattr(obj, attr, None)
            if isinstance(client, (httpx.Client, httpx.AsyncClient)) and str(client.base_url).rstrip("/") == old.rstrip("/"):
                client.base_url = new
                changed += 1
        for client in getattr(obj, "_clients", None) or []:
            if isinstance(client, (httpx.Client, httpx.AsyncClient)) and str(client.base_url).rstrip("/") == old.rstrip("/"):
                client.base_url = new
                changed += 1
        for attr in ("inner", "_inner", "_client_wrapper"):
            visit(getattr(obj, attr, None), depth + 1)

    for value in list(vars(siris).values()):
        visit(value)
    for tool in siris.tools.list():
        visit(tool)
        visit(getattr(tool, "_client", None))
    return changed


def _use(siris: "SirisAI", item: dict[str, Any], payload: dict[str, Any], trigger: str) -> tuple[Any, str]:
    from app.core import settings_overrides

    result = settings_overrides.update(siris, {payload["field"]: payload["new"]})
    switched = switch(siris, payload["old"], payload["new"])
    from app.upkeep import week_review

    week_review.log(siris, "moved", f"{payload['name']} at {payload['new']}")
    return {**result, "switched_clients": switched}, f"{payload['name']} is now at {payload['new']}"


register_action(SOURCE, "use", _use)
