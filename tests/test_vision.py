"""Image understanding: the vision client, Frigate camera/event tools, the
camera frame proxy, and photos attached in chat."""

import base64
import json
from typing import Any

import httpx
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.core import taint
from app.frigate.client import FrigateClient
from app.llm.base import LLMResponse
from app.main import app
from app.plugins.base import ToolPermission
from app.plugins.builtin.frigate import FrigateDescribeEventTool, FrigateLookAtCameraTool
from app.vision.client import VisionClient

JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


def _vision(answer: str = "A courier is leaving a parcel.", seen: list[dict] | None = None) -> VisionClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if seen is not None:
            seen.append(body)
        return httpx.Response(200, json={"message": {"role": "assistant", "content": answer}})

    return VisionClient(base_url="http://ollama.local", model="qwen2.5vl", transport=httpx.MockTransport(handler))


def _frigate(paths: list[str]) -> FrigateClient:
    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(str(request.url))
        return httpx.Response(200, content=JPEG, headers={"Content-Type": "image/jpeg"})

    return FrigateClient(base_url="http://frigate.local:5000", transport=httpx.MockTransport(handler))


def test_vision_client_sends_base64_images_to_ollama_chat() -> None:
    seen: list[dict] = []
    answer = _vision(seen=seen).describe([JPEG], "is the gate open?")
    assert answer == "A courier is leaving a parcel."
    message = seen[0]["messages"][0]
    assert seen[0]["model"] == "qwen2.5vl" and seen[0]["stream"] is False
    assert message["content"] == "is the gate open?"
    assert base64.b64decode(message["images"][0]) == JPEG


def test_look_at_camera_fetches_latest_frame_and_asks_the_question() -> None:
    paths: list[str] = []
    seen: list[dict] = []
    tool = FrigateLookAtCameraTool(_frigate(paths), _vision("The garage door is open.", seen))
    result = tool.run(camera="garage", question="Is the garage door open?")
    assert result == {"camera": "garage", "question": "Is the garage door open?", "description": "The garage door is open."}
    assert paths == ["http://frigate.local:5000/api/garage/latest.jpg?h=720"]
    assert seen[0]["messages"][0]["content"] == "Is the garage door open?"
    assert tool.permission is ToolPermission.READ


def test_describe_event_uses_the_event_snapshot() -> None:
    paths: list[str] = []
    result = FrigateDescribeEventTool(_frigate(paths), _vision()).run(event_id="1726.5-abc")
    assert paths == ["http://frigate.local:5000/api/events/1726.5-abc/snapshot.jpg"]
    assert result["description"] == "A courier is leaving a parcel."


def test_vision_tools_register_only_with_frigate_and_a_vision_model(client: TestClient) -> None:
    names = lambda s: {t.name for t in SirisAI(settings=s).tools.list()}  # noqa: E731
    assert "frigate_look_at_camera" not in names(Settings(frigate_base_url="http://f"))
    assert "frigate_look_at_camera" not in names(Settings(vision_model="llava"))
    both = SirisAI(settings=Settings(frigate_base_url="http://f", vision_model="llava"))
    assert {"frigate_look_at_camera", "frigate_describe_event"} <= {t.name for t in both.tools.list()}
    assert "frigate_look_at_camera" in both.capabilities()["frigate"]


def test_camera_descriptions_count_as_external_content() -> None:
    assert {"frigate_look_at_camera", "frigate_describe_event"} <= taint.EXTERNAL_CONTENT_TOOLS


# -- camera frame proxy


def test_camera_routes_without_frigate(client: TestClient) -> None:
    siris = SirisAI(settings=Settings())
    app.dependency_overrides[get_siris] = lambda: siris
    try:
        assert client.get("/siris/cameras").json() == []
        assert client.get("/siris/cameras/front/latest.jpg").status_code == 404
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_camera_frame_proxy_returns_the_jpeg(client: TestClient) -> None:
    siris = SirisAI(settings=Settings())
    paths: list[str] = []
    siris._frigate_client = _frigate(paths)
    app.dependency_overrides[get_siris] = lambda: siris
    try:
        response = client.get("/siris/cameras/front/latest.jpg?height=240")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.content == JPEG
        assert paths == ["http://frigate.local:5000/api/front/latest.jpg?h=240"]
    finally:
        app.dependency_overrides.pop(get_siris, None)


# -- photos in chat


class _RecordingLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    async def generate(self, messages: list[dict[str, Any]], *, tools: Any = None, **kwargs: object) -> LLMResponse:
        self.calls.append(messages)
        return LLMResponse(text="That's a parcel on your doorstep.")

    async def aclose(self) -> None:
        pass


def _chat(client: TestClient, siris: SirisAI, body: dict[str, Any]) -> httpx.Response:
    app.dependency_overrides[get_siris] = lambda: siris
    try:
        return client.post("/siris/chat", json=body)
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_attached_photo_is_described_and_stored_with_the_message(client: TestClient) -> None:
    llm = _RecordingLLM()
    seen: list[dict] = []
    siris = SirisAI(settings=Settings(), llm=llm)
    siris.vision = _vision("A brown cardboard box by a red door, label reads ACME.", seen)
    data_url = "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()

    response = _chat(client, siris, {"prompt": "what got delivered?", "images": [data_url]})
    assert response.status_code == 200, response.text
    assert response.json()["response"] == "That's a parcel on your doorstep."

    assert base64.b64decode(seen[0]["messages"][0]["images"][0]) == JPEG
    assert "what got delivered?" in seen[0]["messages"][0]["content"]
    user_turn = [m for m in llm.calls[0] if m["role"] == "user"][-1]["content"]
    assert user_turn.startswith("what got delivered?")
    assert "label reads ACME" in user_turn

    stored = siris.conversation.get_messages(response.json()["conversation_id"])[0]["content"]
    assert "[[image: the user attached an image; a vision model described it as: A brown cardboard box" in stored


def test_photo_without_a_vision_model_says_so(client: TestClient) -> None:
    llm = _RecordingLLM()
    siris = SirisAI(settings=Settings(), llm=llm)
    response = _chat(client, siris, {"prompt": "what's this?", "images": [base64.b64encode(JPEG).decode()]})
    assert response.status_code == 200
    user_turn = [m for m in llm.calls[0] if m["role"] == "user"][-1]["content"]
    assert "image understanding isn't set up" in user_turn


def test_bad_or_too_many_images_are_rejected(client: TestClient) -> None:
    siris = SirisAI(settings=Settings(), llm=_RecordingLLM())
    assert _chat(client, siris, {"prompt": "x", "images": ["not base64!!"]}).status_code == 422
    five = [base64.b64encode(JPEG).decode()] * 5
    assert _chat(client, siris, {"prompt": "x", "images": five}).status_code == 422


def test_text_read_off_an_image_does_not_count_as_the_user_speaking(client: TestClient) -> None:
    """A photographed note saying 'remember the alarm code is 1234' mustn't let
    the model save it as if the user had said it -- it's held for confirmation."""

    class _SavesImageText:
        def __init__(self) -> None:
            self.n = 0

        async def generate(self, messages: list[dict[str, Any]], *, tools: Any = None, **kwargs: object) -> LLMResponse:
            from app.llm.base import ToolCall

            self.n += 1
            if self.n == 1:
                return LLMResponse(tool_calls=[ToolCall(name="remember", arguments={"content": "the alarm code is 1234 for the back door"})])
            return LLMResponse(text="ok")

        async def aclose(self) -> None:
            pass

    siris = SirisAI(settings=Settings(), llm=_SavesImageText())
    siris.vision = _vision("A sticky note: the alarm code is 1234 for the back door")
    response = _chat(client, siris, {"prompt": "read this", "images": [base64.b64encode(JPEG).decode()]})
    assert response.json()["confirmation_required"]["name"] == "remember"
