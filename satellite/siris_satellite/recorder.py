"""When an utterance starts and ends, decided chunk by chunk.

Pure logic with no audio dependencies -- audio_io.py decides whether each
chunk contains speech (Silero VAD, or an amplitude check as a fallback) and
feeds the verdict in here, so the start/stop rules are testable without a
microphone:

- Wait up to `start_timeout` seconds for speech to begin; give up (TIMEOUT)
  if it doesn't -- that's how a follow-up window closes.
- Keep a little audio from just before speech was detected (pre-roll) so the
  first syllable isn't clipped.
- Once speaking, stop after `silence_timeout` seconds of non-speech, or at
  `max_seconds` regardless.
"""

from collections import deque
from typing import Any

CONTINUE = "continue"
DONE = "done"
TIMEOUT = "timeout"


class UtteranceRecorder:
    def __init__(
        self,
        chunk_seconds: float,
        silence_timeout: float,
        max_seconds: float,
        start_timeout: float,
        pre_roll_seconds: float = 0.3,
    ) -> None:
        self._chunk_seconds = chunk_seconds
        self._silence_chunks_limit = max(1, round(silence_timeout / chunk_seconds))
        self._max_chunks = max(1, round(max_seconds / chunk_seconds))
        self._start_chunks_limit = max(1, round(start_timeout / chunk_seconds))
        self._pre_roll: deque[Any] = deque(maxlen=max(1, round(pre_roll_seconds / chunk_seconds)))
        self.frames: list[Any] = []
        self.speech_started = False
        self._waited = 0
        self._silent = 0

    def feed(self, chunk: Any, is_speech: bool) -> str:
        if not self.speech_started:
            self._pre_roll.append(chunk)
            if is_speech:
                self.speech_started = True
                self.frames.extend(self._pre_roll)
                return CONTINUE
            self._waited += 1
            return TIMEOUT if self._waited >= self._start_chunks_limit else CONTINUE

        self.frames.append(chunk)
        self._silent = 0 if is_speech else self._silent + 1
        if self._silent >= self._silence_chunks_limit or len(self.frames) >= self._max_chunks:
            return DONE
        return CONTINUE
