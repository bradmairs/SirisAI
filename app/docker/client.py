"""Thin client for the Docker Engine API
(https://docs.docker.com/reference/api/engine/).

Synchronous on purpose -- Tool.run() is a synchronous interface (see
app/plugins/base.py). Talks to the API directly over HTTP rather than
pulling in the docker-py SDK, same "thin REST client" pattern as
HomeAssistantClient. Supports both a Unix socket (the host's
/var/run/docker.sock mounted into this container) and a TCP-exposed daemon.
"""

import httpx


class DockerClient:
    def __init__(self, base_url: str, timeout: float = 10.0, transport: httpx.BaseTransport | None = None) -> None:
        if transport is not None:
            self._client = httpx.Client(base_url="http://docker", transport=transport, timeout=timeout)
        elif base_url.startswith("unix://"):
            socket_path = base_url.removeprefix("unix://")
            uds_transport = httpx.HTTPTransport(uds=socket_path)
            # Docker ignores the host on a unix socket; httpx still needs
            # *some* scheme+host in base_url to resolve relative paths against.
            self._client = httpx.Client(base_url="http://docker", transport=uds_transport, timeout=timeout)
        else:
            self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def list_containers(self, all_containers: bool = True) -> list[dict]:
        response = self._client.get("/containers/json", params={"all": "true" if all_containers else "false"})
        response.raise_for_status()
        return [
            {
                "id": c["Id"][:12],
                "names": [n.lstrip("/") for n in c.get("Names", [])],
                "image": c.get("Image"),
                "state": c.get("State"),
                "status": c.get("Status"),
            }
            for c in response.json()
        ]

    def restart_container(self, container: str, timeout_seconds: int = 10) -> dict:
        response = self._client.post(f"/containers/{container}/restart", params={"t": timeout_seconds})
        response.raise_for_status()
        return {"container": container, "restarted": True}

    def close(self) -> None:
        self._client.close()
