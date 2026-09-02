"""Model-agnostic speech interfaces -- the voice equivalent of
app/llm/base.py. SirisAI's own container stays lightweight: these talk to
an external service over HTTP (same pattern as OllamaClient) rather than
loading Whisper/Piper model weights in-process.
"""

from abc import ABC, abstractmethod


class SpeechToTextClient(ABC):
    """Transcribes audio to text."""

    @abstractmethod
    async def transcribe(self, audio: bytes, *, filename: str = "audio.wav", content_type: str = "audio/wav") -> str:
        """Return a transcript of `audio`. Raises on backend failure."""
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release any resources (connections, etc). No-op by default."""


class TextToSpeechClient(ABC):
    """Synthesizes speech audio from text."""

    @abstractmethod
    async def synthesize(self, text: str) -> bytes:
        """Return audio bytes (MP3) for `text`. Raises on backend failure."""
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release any resources (connections, etc). No-op by default."""


class NullSpeechToTextClient(SpeechToTextClient):
    """Placeholder used until a real STT backend is configured."""

    async def transcribe(self, audio: bytes, *, filename: str = "audio.wav", content_type: str = "audio/wav") -> str:
        raise RuntimeError("No speech-to-text backend configured yet.")


class NullTextToSpeechClient(TextToSpeechClient):
    """Placeholder used until a real TTS backend is configured."""

    async def synthesize(self, text: str) -> bytes:
        raise RuntimeError("No text-to-speech backend configured yet.")
