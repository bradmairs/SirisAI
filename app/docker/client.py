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

    def published_ports(self) -> dict[str, list[int]]:
        """Each running container's ports published on the host (6.48, for the network map)."""
        response = self._client.get("/containers/json", params={"all": "false"})
        response.raise_for_status()
        return {
            (c.get("Names") or ["/" + c["Id"][:12]])[0].lstrip("/"): sorted({p["PublicPort"] for p in c.get("Ports") or [] if p.get("PublicPort")})
            for c in response.json()
        }

    def restart_container(self, container: str, timeout_seconds: int = 10) -> dict:
        response = self._client.post(f"/containers/{container}/restart", params={"t": timeout_seconds})
        response.raise_for_status()
        return {"container": container, "restarted": True}

    def get_container_logs(self, container: str, tail: int = 200) -> str:
        response = self._client.get(
            f"/containers/{container}/logs",
            params={"stdout": "true", "stderr": "true", "tail": str(tail), "timestamps": "true"},
        )
        response.raise_for_status()
        return _demux_log_stream(response.content)

    # -- updating containers (app/homelab/containers.py) --------------------------

    def _json(self, method: str, path: str, **kwargs) -> dict:
        response = self._client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}

    def inspect_container(self, container: str) -> dict:
        return self._json("GET", f"/containers/{container}/json")

    def inspect_image(self, image: str) -> dict:
        return self._json("GET", f"/images/{image}/json")

    def remote_digest(self, image: str) -> str | None:
        """The registry's current digest for an image reference, asked through
        the daemon (so it uses the daemon's own registry logins)."""
        return (self._json("GET", f"/distribution/{image}/json").get("Descriptor") or {}).get("digest")

    def pull(self, image: str, timeout: float = 900.0) -> None:
        name, tag = _split_ref(image)
        response = self._client.post("/images/create", params={"fromImage": name, "tag": tag}, timeout=timeout)
        response.raise_for_status()
        # The body streams progress as JSON lines; a failure shows up there, not in the status.
        for line in response.text.splitlines():
            if '"error"' in line:
                raise RuntimeError(line[-300:])

    def stop(self, container: str, timeout_seconds: int = 20) -> None:
        response = self._client.post(f"/containers/{container}/stop", params={"t": timeout_seconds}, timeout=timeout_seconds + 30)
        if response.status_code not in (204, 304):
            response.raise_for_status()

    def start(self, container: str) -> None:
        response = self._client.post(f"/containers/{container}/start")
        if response.status_code not in (204, 304):
            response.raise_for_status()

    def rename(self, container: str, name: str) -> None:
        self._client.post(f"/containers/{container}/rename", params={"name": name}).raise_for_status()

    def create(self, name: str, config: dict) -> str:
        return self._json("POST", "/containers/create", params={"name": name}, json=config)["Id"]

    def connect(self, network: str, container: str, endpoint: dict) -> None:
        self._client.post(f"/networks/{network}/connect", json={"Container": container, "EndpointConfig": endpoint}).raise_for_status()

    def remove(self, container: str, force: bool = True) -> None:
        response = self._client.delete(f"/containers/{container}", params={"force": "true" if force else "false"})
        if response.status_code != 404:
            response.raise_for_status()

    def close(self) -> None:
        self._client.close()


def _split_ref(image: str) -> tuple[str, str]:
    """'lscr.io/linuxserver/plex:latest' -> ('lscr.io/linuxserver/plex', 'latest');
    a registry port isn't a tag ('host:5000/app' -> latest)."""
    name, sep, tag = image.rpartition(":")
    if not sep or "/" in tag:
        return image, "latest"
    return name, tag


def _demux_log_stream(raw: bytes) -> str:
    """Docker's logs endpoint multiplexes stdout/stderr behind an 8-byte
    header (1-byte stream type + 3 zero bytes + 4-byte big-endian payload
    length) before each chunk -- but only for a container created without
    a TTY; a TTY container's logs come back as plain, unframed text
    instead. There's no separate signal for which one this response is,
    so this treats "first byte isn't a valid stream type" as "not framed"
    and falls back to decoding the raw bytes untouched."""
    if len(raw) < 8 or raw[0] not in (0, 1, 2):
        return raw.decode("utf-8", errors="replace")

    chunks: list[str] = []
    offset = 0
    while offset + 8 <= len(raw):
        stream_type = raw[offset]
        if stream_type not in (0, 1, 2):
            break
        length = int.from_bytes(raw[offset + 4 : offset + 8], "big")
        payload_start = offset + 8
        payload_end = payload_start + length
        if payload_end > len(raw):
            break
        chunks.append(raw[payload_start:payload_end].decode("utf-8", errors="replace"))
        offset = payload_end

    return "".join(chunks) if chunks else raw.decode("utf-8", errors="replace")
