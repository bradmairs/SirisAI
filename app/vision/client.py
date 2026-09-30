"""Image understanding through a local vision-language model served by
Ollama (e.g. qwen2.5vl, llava, gemma3) -- what turns a Frigate "person
detected" into "a courier leaving a parcel on the step", and what reads a
photo attached in chat.

Synchronous like the other tool-facing HTTP clients (tools run on a worker
thread). A separate client from the chat LLM on purpose: vision models are a
different, usually slower model, and only images go to it.
"""

import base64

import httpx

DEFAULT_PROMPT = (
    "Describe what you see in this image in two or three plain sentences: people, vehicles, animals, "
    "packages, open doors or gates, and anything unusual. Say so plainly if nothing notable is there."
)


class VisionClient:
    def __init__(self, base_url: str, model: str, timeout: float = 120.0, transport: httpx.BaseTransport | None = None) -> None:
        self.model = model
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)

    def describe(self, images: list[bytes], prompt: str | None = None) -> str:
        """The model's answer about `images` (JPEG/PNG bytes) for `prompt`."""
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [
                {
                    "role": "user",
                    "content": prompt or DEFAULT_PROMPT,
                    "images": [base64.b64encode(image).decode("ascii") for image in images],
                }
            ],
        }
        response = self._client.post("/api/chat", json=payload)
        response.raise_for_status()
        return (response.json().get("message", {}).get("content") or "").strip()

    def close(self) -> None:
        self._client.close()
