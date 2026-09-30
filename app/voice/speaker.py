"""Who is speaking: a voiceprint (speaker embedding) for a recording, and
matching it against enrolled household members.

The encoder is Resemblyzer's pretrained speaker-verification network (a
3-layer LSTM over 40-band mel spectrograms, Apache-2.0) run in plain numpy
-- its weights are converted once by scripts/fetch_voice_encoder.py, so
SirisAI needs neither PyTorch nor a separate service for this. Everything
here mirrors Resemblyzer's own preprocessing and inference:
volume-normalise to -30 dBFS, trim long silences, mel spectrogram (25ms
window, 10ms hop), 1.6s partial windows at 1.3 per second, one embedding
per window, averaged and L2-normalised. Two differences, both minor: long
silences are found with an energy check rather than webrtcvad, and audio at
other sample rates is resampled by linear interpolation.

Only WAV input is decoded natively (what the voice satellite and the web
UI's enrolment recorder send); anything else is converted with ffmpeg when
it's installed, and otherwise simply isn't identified.
"""

import io
import logging
import shutil
import subprocess
import wave
from functools import lru_cache
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
_N_FFT = 400  # 25ms
_HOP = 160  # 10ms
_N_MELS = 40
_PARTIAL_FRAMES = 160
_HIDDEN = 256
_TARGET_DBFS = -30.0
_INT16_MAX = 2**15 - 1
MIN_SPEECH_SECONDS = 1.0


def decode_audio(data: bytes) -> np.ndarray | None:
    """Mono float32 samples in [-1, 1] at 16kHz, or None if undecodable."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            channels, width, rate = wav.getnchannels(), wav.getsampwidth(), wav.getframerate()
            frames = wav.readframes(wav.getnframes())
        if width == 2:
            samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
        elif width == 4:
            samples = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
        elif width == 1:
            samples = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        else:
            return None
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        return _resample(samples, rate)
    except (wave.Error, EOFError, ValueError):
        pass
    return _decode_with_ffmpeg(data)


def _decode_with_ffmpeg(data: bytes) -> np.ndarray | None:
    if shutil.which("ffmpeg") is None:
        return None
    try:
        result = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1"],
            input=data,
            capture_output=True,
            timeout=30,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return np.frombuffer(result.stdout, dtype="<i2").astype(np.float32) / 32768.0


def _resample(samples: np.ndarray, rate: int) -> np.ndarray:
    if rate == SAMPLE_RATE or samples.size == 0:
        return samples.astype(np.float32)
    duration = samples.size / rate
    target = np.linspace(0, duration, int(round(duration * SAMPLE_RATE)), endpoint=False)
    source = np.arange(samples.size) / rate
    return np.interp(target, source, samples).astype(np.float32)


def normalize_volume(wav: np.ndarray) -> np.ndarray:
    rms = np.sqrt(np.mean((wav * _INT16_MAX) ** 2))
    if rms <= 0:
        return wav
    change = _TARGET_DBFS - 20 * np.log10(rms / _INT16_MAX)
    return wav if change < 0 else wav * (10 ** (change / 20))


def trim_long_silences(wav: np.ndarray) -> np.ndarray:
    """Drop stretches of silence longer than ~0.2s, keeping speech and the
    short pauses inside it (Resemblyzer's windowing and smoothing, with an
    energy threshold standing in for webrtcvad)."""
    window = 30 * SAMPLE_RATE // 1000
    wav = wav[: wav.size - (wav.size % window)]
    if wav.size == 0:
        return wav
    frames = wav.reshape(-1, window)
    energy_db = 10 * np.log10(np.mean(frames**2, axis=1) + 1e-10)
    voiced = (energy_db > max(energy_db.max() - 35.0, -55.0)).astype(float)
    width = 8
    padded = np.concatenate((np.zeros((width - 1) // 2), voiced, np.zeros(width // 2)))
    smoothed = np.convolve(padded, np.ones(width) / width, mode="valid")
    mask = np.round(smoothed).astype(bool)
    dilated = np.convolve(mask.astype(int), np.ones(7, dtype=int), mode="same") > 0
    return wav[np.repeat(dilated, window)]


@lru_cache(maxsize=1)
def _mel_filterbank() -> np.ndarray:
    """librosa.filters.mel(sr=16000, n_fft=400, n_mels=40) -- Slaney mel
    scale and area normalisation, librosa's defaults."""

    def hz_to_mel(hz: np.ndarray) -> np.ndarray:
        hz = np.asanyarray(hz, dtype=float)
        mel = hz / (200.0 / 3)
        log_region = hz >= 1000.0
        mel = np.where(log_region, 15.0 + np.log(np.maximum(hz, 1e-10) / 1000.0) / (np.log(6.4) / 27.0), mel)
        return mel

    def mel_to_hz(mel: np.ndarray) -> np.ndarray:
        hz = (200.0 / 3) * mel
        return np.where(mel >= 15.0, 1000.0 * np.exp((np.log(6.4) / 27.0) * (mel - 15.0)), hz)

    fft_freqs = np.linspace(0, SAMPLE_RATE / 2, 1 + _N_FFT // 2)
    mel_f = mel_to_hz(np.linspace(hz_to_mel(0.0), hz_to_mel(SAMPLE_RATE / 2), _N_MELS + 2))
    fdiff = np.diff(mel_f)
    ramps = np.subtract.outer(mel_f, fft_freqs)
    weights = np.zeros((_N_MELS, fft_freqs.size))
    for i in range(_N_MELS):
        lower = -ramps[i] / fdiff[i]
        upper = ramps[i + 2] / fdiff[i + 1]
        weights[i] = np.maximum(0, np.minimum(lower, upper))
    weights *= (2.0 / (mel_f[2 : _N_MELS + 2] - mel_f[:_N_MELS]))[:, None]
    return weights.astype(np.float32)


def mel_spectrogram(wav: np.ndarray) -> np.ndarray:
    """(frames, 40) power mel spectrogram, like librosa.feature.melspectrogram
    with center=True."""
    padded = np.pad(wav, _N_FFT // 2, mode="reflect")
    n_frames = 1 + (padded.size - _N_FFT) // _HOP
    idx = np.arange(_N_FFT)[None, :] + _HOP * np.arange(n_frames)[:, None]
    window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(_N_FFT) / _N_FFT)
    power = np.abs(np.fft.rfft(padded[idx] * window, axis=1)) ** 2
    return (power @ _mel_filterbank().T).astype(np.float32)


def partial_slices(n_samples: int, rate: float = 1.3, min_coverage: float = 0.75) -> list[slice]:
    samples_per_frame = _HOP
    n_frames = int(np.ceil((n_samples + 1) / samples_per_frame))
    frame_step = int(np.round((SAMPLE_RATE / rate) / samples_per_frame))
    steps = max(1, n_frames - _PARTIAL_FRAMES + frame_step + 1)
    mel_slices = [slice(i, i + _PARTIAL_FRAMES) for i in range(0, steps, frame_step)]
    last = mel_slices[-1]
    coverage = (n_samples - last.start * samples_per_frame) / (_PARTIAL_FRAMES * samples_per_frame)
    if coverage < min_coverage and len(mel_slices) > 1:
        mel_slices = mel_slices[:-1]
    return mel_slices


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + np.tanh(0.5 * x))


class SpeakerEncoder:
    def __init__(self, weights: dict[str, np.ndarray]) -> None:
        self._w = weights

    @classmethod
    def load(cls, path: str | Path) -> "SpeakerEncoder":
        with np.load(path) as data:
            return cls({key: data[key].astype(np.float32) for key in data.files})

    def _forward(self, mels: np.ndarray) -> np.ndarray:
        """(batch, frames, 40) -> (batch, 256) L2-normalised embeddings. A
        standard PyTorch LSTM (gates in i, f, g, o order), final hidden state
        of the last layer -> linear -> ReLU -> L2 norm."""
        x = mels
        hidden = np.zeros((mels.shape[0], _HIDDEN), dtype=np.float32)
        for layer in range(3):
            w_ih, w_hh = self._w[f"lstm.weight_ih_l{layer}"], self._w[f"lstm.weight_hh_l{layer}"]
            bias = self._w[f"lstm.bias_ih_l{layer}"] + self._w[f"lstm.bias_hh_l{layer}"]
            projected = x @ w_ih.T + bias
            hidden = np.zeros((mels.shape[0], _HIDDEN), dtype=np.float32)
            cell = np.zeros_like(hidden)
            outputs = np.empty((mels.shape[0], mels.shape[1], _HIDDEN), dtype=np.float32)
            for t in range(mels.shape[1]):
                gates = projected[:, t] + hidden @ w_hh.T
                i, f, g, o = np.split(gates, 4, axis=1)
                cell = _sigmoid(f) * cell + _sigmoid(i) * np.tanh(g)
                hidden = _sigmoid(o) * np.tanh(cell)
                outputs[:, t] = hidden
            x = outputs
        raw = np.maximum(0.0, hidden @ self._w["linear.weight"].T + self._w["linear.bias"])
        norms = np.linalg.norm(raw, axis=1, keepdims=True)
        return raw / np.where(norms == 0, 1, norms)

    def embed_samples(self, wav: np.ndarray) -> np.ndarray | None:
        wav = trim_long_silences(normalize_volume(wav))
        if wav.size < MIN_SPEECH_SECONDS * SAMPLE_RATE:
            return None
        slices = partial_slices(wav.size)
        needed = slices[-1].stop * _HOP
        if needed >= wav.size:
            wav = np.pad(wav, (0, needed - wav.size))
        mel = mel_spectrogram(wav)
        partials = self._forward(np.stack([mel[s] for s in slices]))
        mean = partials.mean(axis=0)
        return mean / np.linalg.norm(mean)

    def embed(self, audio: bytes) -> list[float] | None:
        """A voiceprint for a recording, or None if it can't be decoded or
        has under a second of speech."""
        samples = decode_audio(audio)
        if samples is None:
            return None
        embedding = self.embed_samples(samples)
        return None if embedding is None else embedding.astype(float).tolist()


def cosine(a: list[float], b: list[float]) -> float:
    va, vb = np.asarray(a), np.asarray(b)
    denom = np.linalg.norm(va) * np.linalg.norm(vb)
    return float(va @ vb / denom) if denom else 0.0
