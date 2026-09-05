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

    def get_container_logs(self, container: str, tail: int = 200) -> str:
        response = self._client.get(
            f"/containers/{container}/logs",
            params={"stdout": "true", "stderr": "true", "tail": str(tail), "timestamps": "true"},
        )
        response.raise_for_status()
        return _demux_log_stream(response.content)

    def close(self) -> None:
        self._client.close()


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
