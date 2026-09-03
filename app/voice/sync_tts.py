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

import httpx


def synthesize(
    base_url: str,
    model: str,
    voice: str,
    text: str,
    timeout: float = 60.0,
    transport: httpx.BaseTransport | None = None,
) -> bytes:
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport) as client:
        response = client.post(
            "/v1/audio/speech", json={"model": model, "voice": voice, "input": text, "response_format": "mp3"}
        )
        response.raise_for_status()
        return response.content
