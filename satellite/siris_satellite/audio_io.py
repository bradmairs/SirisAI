"""Real microphone/speaker I/O: sounddevice for capture, openWakeWord for
offline wake-word detection, Silero VAD (bundled with openWakeWord) to tell
speech from background noise, mpg123 for playback. Only imported by
__main__.py -- satellite.py's orchestration logic doesn't depend on this
module, so that logic stays testable without PortAudio or a wake-word
model installed. Not exercised by this repo's test suite for the same
reason: there's no real microphone/speaker in a CI or dev-container
environment to test it against.

When to stop recording is decided by recorder.py (tested); this module only
answers "is this chunk speech?". Silero is used when its model is available
(`python -c "import openwakeword.utils; openwakeword.utils.download_models()"`
fetches it along with the wake-word models); otherwise a plain loudness
check stands in, as before.

Playback shells out to `mpg123` rather than pulling in an MP3-decoding
dependency -- install it with `apt install mpg123` (or your distro's
equivalent) on the satellite device. stop() kills that process, which is
how an interrupted reply goes quiet immediately.
"""

import io
import logging
import subprocess
import tempfile
import threading
import wave

import numpy as np
import sounddevice as sd
from openwakeword.model import Model as WakeWordModel

from siris_satellite.recorder import CONTINUE, DONE, UtteranceRecorder

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 1280  # openWakeWord expects 80ms chunks at 16kHz
_SILENCE_AMPLITUDE_THRESHOLD = 100


class AmplitudeSpeechDetector:
    def __init__(self, threshold: float = _SILENCE_AMPLITUDE_THRESHOLD) -> None:
        self._threshold = threshold

    def is_speech(self, chunk: np.ndarray) -> bool:
        return float(np.abs(chunk).mean()) >= self._threshold


class SileroSpeechDetector:
    def __init__(self, threshold: float = 0.5) -> None:
        from openwakeword.vad import VAD

        self._vad = VAD()
        self._threshold = threshold

    def is_speech(self, chunk: np.ndarray) -> bool:
        # 640-sample frames: how openWakeWord itself feeds this model, and an
        # 80ms chunk splits into exactly two of them.
        return float(self._vad.predict(chunk.flatten(), frame_size=640)) >= self._threshold


def build_speech_detector(kind: str, threshold: float) -> AmplitudeSpeechDetector | SileroSpeechDetector:
    if kind == "silero":
        try:
            return SileroSpeechDetector(threshold)
        except Exception:
            logger.warning("Silero VAD unavailable (run openwakeword.utils.download_models()); using a loudness check")
    return AmplitudeSpeechDetector()


class MicrophoneInput:
    def __init__(
        self,
        wake_word_model: str,
        wake_word_threshold: float,
        sample_rate: int,
        silence_timeout_seconds: float,
        max_recording_seconds: float,
        speech_start_timeout_seconds: float = 5.0,
        vad: str = "silero",
        vad_threshold: float = 0.5,
        device: int | None = None,
    ) -> None:
        self._sample_rate = sample_rate
        self._silence_timeout_seconds = silence_timeout_seconds
        self._max_recording_seconds = max_recording_seconds
        self._speech_start_timeout_seconds = speech_start_timeout_seconds
        self._device = device
        self._threshold = wake_word_threshold
        self._wake_model = WakeWordModel(wakeword_models=[wake_word_model])
        self._detector = build_speech_detector(vad, vad_threshold)
        self._mic_lock = threading.Lock()

    def _stream(self) -> sd.InputStream:
        return sd.InputStream(
            samplerate=self._sample_rate, channels=1, dtype="int16", blocksize=_CHUNK_SIZE, device=self._device
        )

    def listen_for_wake_word(self, stop: threading.Event | None = None) -> bool:
        with self._mic_lock, self._stream() as stream:
            while stop is None or not stop.is_set():
                chunk, _ = stream.read(_CHUNK_SIZE)
                scores = self._wake_model.predict(chunk.flatten())
                if any(score >= self._threshold for score in scores.values()):
                    # Clear the model's rolling buffer so the same utterance
                    # doesn't fire again on the next listen.
                    self._wake_model.reset()
                    return True
        return False

    def record_utterance(self, start_timeout: float | None = None) -> bytes | None:
        chunk_seconds = _CHUNK_SIZE / self._sample_rate
        recorder = UtteranceRecorder(
            chunk_seconds=chunk_seconds,
            silence_timeout=self._silence_timeout_seconds,
            max_seconds=self._max_recording_seconds,
            start_timeout=start_timeout if start_timeout is not None else self._speech_start_timeout_seconds,
        )
        with self._mic_lock, self._stream() as stream:
            while True:
                chunk, _ = stream.read(_CHUNK_SIZE)
                chunk = chunk.copy()
                status = recorder.feed(chunk, self._detector.is_speech(chunk))
                if status == CONTINUE:
                    continue
                if status == DONE:
                    break
                return None

        audio = np.concatenate(recorder.frames) if recorder.frames else np.zeros((0, 1), dtype="int16")
        return _wav_bytes(audio, self._sample_rate)


class SpeakerOutput:
    def __init__(self, device: int | None = None) -> None:
        self._device = device
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    def play(self, audio: bytes) -> None:
        with tempfile.NamedTemporaryFile(suffix=".mp3") as f:
            f.write(audio)
            f.flush()
            args = ["mpg123", "-q"]
            if self._device is not None:
                args += ["-a", str(self._device)]
            with self._lock:
                self._process = subprocess.Popen([*args, f.name])
                process = self._process
            process.wait()
            with self._lock:
                self._process = None

    def stop(self) -> None:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                self._process.terminate()


def _wav_bytes(audio: np.ndarray, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(audio.tobytes())
    return buffer.getvalue()
