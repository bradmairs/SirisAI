"""Real microphone/speaker I/O: sounddevice for capture/playback,
openWakeWord for offline wake-word detection. Only imported by
__main__.py -- satellite.py's orchestration logic doesn't depend on this
module, so that logic stays testable without PortAudio or a wake-word
model installed. Not exercised by this repo's test suite for the same
reason: there's no real microphone/speaker in a CI or dev-container
environment to test it against.

Playback shells out to `mpg123` rather than pulling in an MP3-decoding
dependency -- install it with `apt install mpg123` (or your distro's
equivalent) on the satellite device.
"""

import io
import subprocess
import tempfile
import wave

import numpy as np
import sounddevice as sd
from openwakeword.model import Model as WakeWordModel

_CHUNK_SIZE = 1280  # openWakeWord expects 80ms chunks at 16kHz
_SILENCE_AMPLITUDE_THRESHOLD = 100


class MicrophoneInput:
    def __init__(
        self,
        wake_word_model: str,
        wake_word_threshold: float,
        sample_rate: int,
        silence_timeout_seconds: float,
        max_recording_seconds: float,
        device: int | None = None,
    ) -> None:
        self._sample_rate = sample_rate
        self._silence_timeout_seconds = silence_timeout_seconds
        self._max_recording_seconds = max_recording_seconds
        self._device = device
        self._threshold = wake_word_threshold
        self._wake_model = WakeWordModel(wakeword_models=[wake_word_model])

    def listen_for_wake_word(self) -> None:
        with sd.InputStream(
            samplerate=self._sample_rate, channels=1, dtype="int16", blocksize=_CHUNK_SIZE, device=self._device
        ) as stream:
            while True:
                chunk, _ = stream.read(_CHUNK_SIZE)
                scores = self._wake_model.predict(chunk.flatten())
                if any(score >= self._threshold for score in scores.values()):
                    return

    def record_utterance(self) -> bytes:
        frames: list[np.ndarray] = []
        silence_chunks = 0
        silence_chunk_limit = int(self._silence_timeout_seconds * self._sample_rate / _CHUNK_SIZE)
        max_chunks = int(self._max_recording_seconds * self._sample_rate / _CHUNK_SIZE)

        with sd.InputStream(
            samplerate=self._sample_rate, channels=1, dtype="int16", blocksize=_CHUNK_SIZE, device=self._device
        ) as stream:
            for _ in range(max_chunks):
                chunk, _ = stream.read(_CHUNK_SIZE)
                frames.append(chunk.copy())
                if np.abs(chunk).mean() < _SILENCE_AMPLITUDE_THRESHOLD:
                    silence_chunks += 1
                    if silence_chunks >= silence_chunk_limit:
                        break
                else:
                    silence_chunks = 0

        audio = np.concatenate(frames) if frames else np.zeros((0, 1), dtype="int16")
        return _wav_bytes(audio, self._sample_rate)


class SpeakerOutput:
    def __init__(self, device: int | None = None) -> None:
        self._device = device

    def play(self, audio: bytes) -> None:
        with tempfile.NamedTemporaryFile(suffix=".mp3") as f:
            f.write(audio)
            f.flush()
            args = ["mpg123", "-q"]
            if self._device is not None:
                args += ["-a", str(self._device)]
            subprocess.run([*args, f.name], check=False)


def _wav_bytes(audio: np.ndarray, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(audio.tobytes())
    return buffer.getvalue()
