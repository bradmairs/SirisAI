"""What runs where, and what depends on what (6.58).

Built from what SirisAI already knows, without scanning the network:

- **servers**: the SSH servers (and cron servers), by address;
- **containers**: the Docker host's running containers and the ports they
  publish, on the server the Docker address points at (or SirisAI's own
  server, SIRISAI_SSH_SELF_SERVER, for a local socket);
- **services**: each integration's address (Home Assistant, Plex, SirisOS,
  Ollama...), matched to the server with that address and, by port, to the
  container serving it;
- **SirisAI** itself, on its own server.

`network_map` describes it; `what_depends_on("nas")` answers "what breaks if
the NAS goes down?": its containers, the services they serve, and SirisAI
when it's SirisAI's own server.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)
LOCAL = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}


def _addr(url: str) -> tuple[str, int | None]:
    u = urlparse(url if "://" in url else f"tcp://{url}")
    return (u.hostname or "").lower(), u.port or ({"https": 443, "http": 80}.get(u.scheme))


def build(siris: "SirisAI") -> dict[str, Any]:
    from app.core.health import _integration_targets

    servers: dict[str, dict[str, Any]] = {}
    if siris.ssh_client is not None:
        for h in siris.ssh_client.hosts:
            servers[h.name] = {"name": h.name, "address": h.host.lower(), "containers": [], "services": []}
    if siris.cron_client is not None:
        for h in siris.cron_client.hosts:
            if not any(s["address"] == h.host.lower() for s in servers.values()):
                servers[h.host] = {"name": h.host, "address": h.host.lower(), "containers": [], "services": []}
    by_address = {s["address"]: s for s in servers.values()}
    own = (siris.settings.ssh_self_server or "").strip().lower()
    own_server = servers.get(own) or next((s for s in servers.values() if s["name"].lower() == own), None)

    docker_host = None
    containers: dict[str, list[int]] = {}
    url = siris.settings.docker_base_url
    client = getattr(siris, "_docker_client", None)
    if client is not None:
        address, _ = _addr(url) if url and not url.startswith("unix://") else ("", None)
        docker_host = by_address.get(address) if address else own_server
        try:
            containers = client.published_ports()
        except Exception as exc:  # noqa: BLE001 - the map still shows the rest
            logger.info("Couldn't list containers for the network map: %s", exc)
    if docker_host is not None:
        docker_host["containers"] = sorted(containers)

    services = []
    for name, target in _integration_targets(siris.settings):
        host, port = _addr(target)
        server = by_address.get(host)
        if server is None and host in LOCAL:
            server = own_server
        container = None
        if server is not None and server is docker_host and port:
            container = next((c for c, ports in containers.items() if port in ports), None)
        services.append({"name": name, "host": host, "port": port, "server": server["name"] if server else None, "container": container})
        if server is not None:
            server["services"].append(name)
    return {"servers": list(servers.values()), "services": services, "sirisai_on": own_server["name"] if own_server else None,
            "docker_on": docker_host["name"] if docker_host else None}


def depends_on(siris: "SirisAI", name: str) -> dict[str, Any]:
    m = build(siris)
    key = name.strip().lower()
    server = next((s for s in m["servers"] if key in (s["name"].lower(), s["address"])), None)
    if server is not None:
        services = [s["name"] for s in m["services"] if s["server"] == server["name"]]
        hit = {"what": server["name"], "kind": "server", "containers": server["containers"], "services": services,
               "sirisai": m["sirisai_on"] == server["name"]}
    else:
        one = next((s for s in m["services"] if s["name"].lower() == key), None)
        if one is not None:
            return {"what": one["name"], "kind": "service", "runs_on": one["server"], "in_container": one["container"],
                    "text": f"{one['name']} runs " + (f"in {one['container']} on {one['server']}" if one["container"] else
                                                     f"on {one['server']}" if one["server"] else f"at {one['host']}, which isn't one of the servers I know") + "."}
        svc = [s for s in m["services"] if s["container"] and s["container"].lower() == key]
        if key not in {c.lower() for s in m["servers"] for c in s["containers"]} and not svc:
            raise ValueError(f"I don't know '{name}'. Servers: {', '.join(s['name'] for s in m['servers']) or 'none'}.")
        hit = {"what": name, "kind": "container", "containers": [], "services": [s["name"] for s in svc], "sirisai": False}
    parts = []
    if hit["sirisai"]:
        parts.append("SirisAI itself (and so every automation, reminder and alert)")
    if hit["services"]:
        parts.append(", ".join(hit["services"]))
    if hit["containers"]:
        parts.append(f"{len(hit['containers'])} container{'s' if len(hit['containers']) != 1 else ''} ({', '.join(hit['containers'][:10])}"
                     + (", ..." if len(hit["containers"]) > 10 else "") + ")")
    hit["text"] = (f"If {hit['what']} goes down: " + "; ".join(parts) + ".") if parts else f"Nothing I know of depends on {hit['what']}."
    return hit


def text(siris: "SirisAI") -> str:
    m = build(siris)
    lines = []
    for s in m["servers"]:
        bits = []
        if m["sirisai_on"] == s["name"]:
            bits.append("SirisAI")
        if s["services"]:
            bits.append(", ".join(s["services"]))
        if s["containers"]:
            bits.append(f"{len(s['containers'])} containers")
        lines.append(f"{s['name']} ({s['address']}): " + ("; ".join(bits) or "nothing I know of"))
    elsewhere = [s["name"] for s in m["services"] if not s["server"]]
    if elsewhere:
        lines.append("Elsewhere: " + ", ".join(elsewhere))
    return ". ".join(lines) + "." if lines else "No servers or services set up yet."
