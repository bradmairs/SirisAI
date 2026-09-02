"""Abstractions the satellite's orchestration loop (satellite.py) depends
on, so that loop is unit-testable with fakes -- no real microphone,
speaker, or wake-word model needed to verify it's correct. The concrete,
hardware-backed implementation lives in audio_io.py and is only imported
by __main__.py.
"""

from typing import Protocol


class AudioInput(Protocol):
    def listen_for_wake_word(self) -> None:
        """Block until the wake word is detected."""
        ...

    def record_utterance(self) -> bytes:
        """Record audio (WAV bytes) until silence or a timeout, called
        right after the wake word has fired."""
        ...


class AudioOutput(Protocol):
    def play(self, audio: bytes) -> None:
        """Play back audio bytes (MP3, from /siris/voice/speak) through
        the speaker, blocking until playback finishes."""
        ...
