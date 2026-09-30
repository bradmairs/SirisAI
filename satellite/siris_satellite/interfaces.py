"""Abstractions the satellite's orchestration loop (satellite.py) depends
on, so that loop is unit-testable with fakes -- no real microphone,
speaker, or wake-word model needed to verify it's correct. The concrete,
hardware-backed implementation lives in audio_io.py and is only imported
by __main__.py.
"""

import threading
from typing import Protocol


class AudioInput(Protocol):
    def listen_for_wake_word(self, stop: threading.Event | None = None) -> bool:
        """Block until the wake word is detected (True), or until `stop` is
        set (False) -- the latter is how listening for an interruption
        during a reply ends when the reply finishes on its own."""
        ...

    def record_utterance(self, start_timeout: float | None = None) -> bytes | None:
        """Record one utterance (WAV bytes): wait for speech to start, then
        record until the speaker goes quiet. Returns None if nobody starts
        speaking within `start_timeout` seconds (None = the default wait
        used right after the wake word)."""
        ...


class AudioOutput(Protocol):
    def play(self, audio: bytes) -> None:
        """Play back audio bytes (MP3) through the speaker, blocking until
        playback finishes or stop() is called."""
        ...

    def stop(self) -> None:
        """Cut off whatever is playing right now (called from another
        thread when the user interrupts)."""
        ...
