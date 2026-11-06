"""A synchronous OpenAI-compatible TTS call (mirrors
OpenAICompatibleTTSClient.synthesize()'s exact request shape, just not
async) -- Tool.run() is a synchronous interface (see
HomeAssistantClient's own docstring for the established reasoning:
SirisAI's tools never bridge async/sync just to reuse an async client),
so speak_in_current_room (app/plugins/builtin/satellites.py) needs its
own sync path to the same TTS backend `siris.tts` already talks to,
rather than awaiting it from inside a sync Tool.run(). A second sync
client class felt like more ceremony than one function warrants, for
what's still a single-provider integration.
"""

import threading

import httpx

# One client per TTS server, kept open (4.6): a new client per sentence
# meant a fresh connection (and TLS handshake) every time it spoke.
_clients: dict[tuple[str, float, object], httpx.Client] = {}
_lock = threading.Lock()


def _client(base_url: str, timeout: float, transport: httpx.BaseTransport | None) -> httpx.Client:
    key = (base_url.rstrip("/"), timeout, transport)
    with _lock:
        client = _clients.get(key)
        if client is None or client.is_closed:
            client = _clients[key] = httpx.Client(base_url=key[0], timeout=timeout, transport=transport)
        return client


def synthesize(
    base_url: str,
    model: str,
    voice: str,
    text: str,
    timeout: float = 60.0,
    transport: httpx.BaseTransport | None = None,
) -> bytes:
    response = _client(base_url, timeout, transport).post(
        "/v1/audio/speech", json={"model": model, "voice": voice, "input": text, "response_format": "mp3"}
    )
    response.raise_for_status()
    return response.content
