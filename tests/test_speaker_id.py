"""Speaker recognition: the torch-free weight converter, the numpy encoder,
voiceprint enrolment/identification, and its use on voice turns."""

import io
import json
import pickle
import struct
import sys
import types
import wave
from collections import OrderedDict
from pathlib import Path
from typing import Any

import numpy as np
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.siris import SirisAI, get_siris
from app.core.user_context import get_current_user_id
from app.db.session import SessionLocal
from app.main import app
from app.voice.speaker import (
    SpeakerEncoder,
    decode_audio,
    mel_spectrogram,
    partial_slices,
    trim_long_silences,
    _mel_filterbank,
)
from app.voice.voiceprints import VoiceprintStore

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from fetch_voice_encoder import convert  # noqa: E402


# -- legacy PyTorch checkpoint parsing (scripts/fetch_voice_encoder.py)


def _legacy_checkpoint(tensors: dict[str, np.ndarray]) -> bytes:
    """Write tensors the way torch.save did before 1.6, without torch: fake
    `torch` modules exist only long enough for pickle to name them."""
    torch_mod = types.ModuleType("torch")
    utils_mod = types.ModuleType("torch._utils")

    class FloatStorage:
        pass

    FloatStorage.__module__ = "torch"
    torch_mod.FloatStorage = FloatStorage

    def _rebuild_tensor_v2(*args: Any) -> None:
        raise AssertionError("never called while writing")

    _rebuild_tensor_v2.__module__ = "torch._utils"
    _rebuild_tensor_v2.__qualname__ = "_rebuild_tensor_v2"
    FloatStorage.__qualname__ = "FloatStorage"
    utils_mod._rebuild_tensor_v2 = _rebuild_tensor_v2

    class StorageRef:
        def __init__(self, key: str, size: int) -> None:
            self.key, self.size = key, size

    class TensorRef:
        def __init__(self, storage: StorageRef, shape: tuple[int, ...]) -> None:
            self.storage, self.shape = storage, shape

        def __reduce__(self) -> Any:
            stride = tuple(int(np.prod(self.shape[i + 1 :])) for i in range(len(self.shape)))
            return (_rebuild_tensor_v2, (self.storage, 0, self.shape, stride, False, OrderedDict()))

    class Pickler(pickle.Pickler):
        def persistent_id(self, obj: Any) -> Any:
            if isinstance(obj, StorageRef):
                return ("storage", FloatStorage, obj.key, "cpu", obj.size, None)
            return None

    saved = {name: sys.modules.get(name) for name in ("torch", "torch._utils")}
    sys.modules["torch"], sys.modules["torch._utils"] = torch_mod, utils_mod
    try:
        refs = {name: TensorRef(StorageRef(str(i), arr.size), arr.shape) for i, (name, arr) in enumerate(tensors.items())}
        out = io.BytesIO()
        pickle.dump(0x1950A86A20F9469CFC6C, out, protocol=2)
        pickle.dump(1001, out, protocol=2)
        pickle.dump({"little_endian": True}, out, protocol=2)
        Pickler(out, protocol=2).dump({"model_state": OrderedDict(refs), "step": 7})
        pickle.dump([str(i) for i in range(len(tensors))], out, protocol=2)
        for arr in tensors.values():
            out.write(struct.pack("<q", arr.size))
            out.write(arr.astype("<f4").tobytes())
        return out.getvalue()
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def test_convert_reads_legacy_torch_checkpoints_without_torch() -> None:
    tensors = {"linear.weight": np.arange(6, dtype=np.float32).reshape(2, 3), "linear.bias": np.array([0.5, -1.0], dtype=np.float32)}
    weights = convert(_legacy_checkpoint(tensors))
    assert set(weights) == {"linear.weight", "linear.bias"}
    np.testing.assert_array_equal(weights["linear.weight"], tensors["linear.weight"])
    np.testing.assert_array_equal(weights["linear.bias"], tensors["linear.bias"])


def test_convert_refuses_unexpected_globals() -> None:
    # The object pickle references os.system -- a malicious checkpoint.
    payload = pickle.dumps(0) + pickle.dumps(0) + pickle.dumps({}) + b"cos\nsystem\n."
    try:
        convert(payload)
    except pickle.UnpicklingError:
        return
    raise AssertionError("expected UnpicklingError")


# -- the encoder


def _random_encoder(seed: int = 0) -> SpeakerEncoder:
    rng = np.random.default_rng(seed)
    weights: dict[str, np.ndarray] = {}
    for layer, n_in in enumerate((40, 256, 256)):
        weights[f"lstm.weight_ih_l{layer}"] = rng.normal(0, 0.1, (1024, n_in)).astype(np.float32)
        weights[f"lstm.weight_hh_l{layer}"] = rng.normal(0, 0.1, (1024, 256)).astype(np.float32)
        weights[f"lstm.bias_ih_l{layer}"] = rng.normal(0, 0.1, 1024).astype(np.float32)
        weights[f"lstm.bias_hh_l{layer}"] = rng.normal(0, 0.1, 1024).astype(np.float32)
    weights["linear.weight"] = rng.normal(0, 0.1, (256, 256)).astype(np.float32)
    weights["linear.bias"] = rng.normal(0.05, 0.1, 256).astype(np.float32)
    return SpeakerEncoder(weights)


def _wav(samples: np.ndarray, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def _voice_like(seconds: float, pitch: float, rate: int = 16000, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * rate)) / rate
    tone = sum(np.sin(2 * np.pi * pitch * k * t) / k for k in range(1, 6))
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 3 * t)
    return (0.2 * tone * envelope + 0.01 * rng.normal(size=t.size)).astype(np.float32)


def test_decode_audio_resamples_and_mixes_down() -> None:
    samples = decode_audio(_wav(_voice_like(1.0, 200, rate=22050), rate=22050))
    assert samples is not None
    assert abs(samples.size - 16000) <= 1


def test_mel_filterbank_and_spectrogram_shapes() -> None:
    bank = _mel_filterbank()
    assert bank.shape == (40, 201)
    assert (bank >= 0).all() and (bank.sum(axis=1) > 0).all()
    mel = mel_spectrogram(np.zeros(16000, dtype=np.float32))
    assert mel.shape == (101, 40)


def test_partial_slices_cover_the_utterance() -> None:
    slices = partial_slices(16000 * 3)
    assert slices[0] == slice(0, 160)
    assert all(s.stop - s.start == 160 for s in slices)
    assert len(slices) == 3


def test_trim_long_silences_drops_long_gaps_but_keeps_speech() -> None:
    speech = _voice_like(1.0, 180)
    wav = np.concatenate([speech, np.zeros(16000 * 2, dtype=np.float32), speech])
    trimmed = trim_long_silences(wav)
    assert 2 * 16000 * 0.9 < trimmed.size < wav.size


def test_encoder_embedding_is_normalised_deterministic_and_needs_speech() -> None:
    encoder = _random_encoder()
    audio = _wav(_voice_like(2.5, 150))
    first, second = encoder.embed(audio), encoder.embed(audio)
    assert first is not None and len(first) == 256
    assert abs(np.linalg.norm(first) - 1.0) < 1e-5
    assert min(first) >= 0.0
    assert first == second
    assert encoder.embed(_wav(np.zeros(16000 * 2, dtype=np.float32))) is None


# -- VoiceprintStore


def _unit(vec: list[float]) -> list[float]:
    arr = np.asarray(vec, dtype=float)
    return (arr / np.linalg.norm(arr)).tolist()


def test_enroll_averages_samples_and_identify_needs_threshold_and_margin(client: TestClient) -> None:
    store = VoiceprintStore(SessionLocal)
    users = ("sid_alice", "sid_bob", "sid_c", "sid_d")
    for user in users:
        store.delete(user)
    try:
        store.enroll("sid_alice", _unit([1, 0, 0, 0.1]))
        assert store.enroll("sid_alice", _unit([0.9, 0.1, 0, 0.1])) == {"user_id": "sid_alice", "samples": 2}
        store.enroll("sid_bob", _unit([0, 1, 0, 0.1]))

        match = store.identify(_unit([0.95, 0.05, 0, 0.1]), threshold=0.8)
        assert match["user_id"] == "sid_alice"
        assert match["scores"]["sid_alice"] > match["scores"]["sid_bob"]
        # Similar to nobody enrolled here -> no match.
        assert store.identify(_unit([0, 0, 1, 0]), threshold=0.8)["user_id"] is None
        assert {"sid_alice", "sid_bob"} <= {v["user_id"] for v in store.list_voiceprints()}
        assert store.delete("sid_bob") is True
        assert store.delete("sid_bob") is False
        store.delete("sid_alice")

        # Equally close to two people -> too ambiguous to call.
        store.enroll("sid_c", [1.0, 0, 0, 0])
        store.enroll("sid_d", [0, 1.0, 0, 0])
        assert store.identify(_unit([1, 1, 0, 0]), threshold=0.5)["user_id"] is None
        # A print of another dimension is ignored, not an error.
        assert "sid_c" not in store.identify([1.0] * 8, threshold=0.5)["scores"]
    finally:
        for user in users:
            store.delete(user)


# -- routes


def test_voiceprint_routes_report_501_without_the_model(client: TestClient) -> None:
    siris = SirisAI(settings=Settings(speaker_id_model_path="/nonexistent.npz"))
    app.dependency_overrides[get_siris] = lambda: siris
    try:
        response = client.post("/siris/voice/voiceprints/enroll", files={"file": ("a.wav", b"x", "audio/wav")})
        assert response.status_code == 501
        assert client.get("/siris/voice/voiceprints").status_code == 200
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_enroll_list_identify_delete_through_the_api(client: TestClient, tmp_path: Path) -> None:
    model = tmp_path / "enc.npz"
    np.savez(model, **_random_encoder()._w)
    siris = SirisAI(settings=Settings(speaker_id_model_path=str(model), speaker_id_threshold=0.5))
    siris.voiceprints.delete("api_user")
    app.dependency_overrides[get_siris] = lambda: siris
    try:
        audio = _wav(_voice_like(2.5, 150))
        enrolled = client.post(
            "/siris/voice/voiceprints/enroll", data={"user": "api_user"}, files={"file": ("a.wav", audio, "audio/wav")}
        )
        assert enrolled.status_code == 200, enrolled.text
        assert enrolled.json() == {"user_id": "api_user", "samples": 1}
        assert "api_user" in [v["user_id"] for v in client.get("/siris/voice/voiceprints").json()]

        identified = client.post("/siris/voice/identify", files={"file": ("a.wav", audio, "audio/wav")}).json()
        assert identified["scores"]["api_user"] > 0.99

        silent = client.post(
            "/siris/voice/voiceprints/enroll", files={"file": ("s.wav", _wav(np.zeros(32000, dtype=np.float32)), "audio/wav")}
        )
        assert silent.status_code == 422
        assert client.delete("/siris/voice/voiceprints/api_user").json() == {"user_id": "api_user", "deleted": True}
        assert client.delete("/siris/voice/voiceprints/api_user").status_code == 404
    finally:
        app.dependency_overrides.pop(get_siris, None)


class _FakeSTT:
    async def transcribe(self, audio: bytes, *, filename: str = "a.wav", content_type: str = "audio/wav") -> str:
        return "what do you know about me"

    async def aclose(self) -> None:
        pass


class _EchoUserLLM:
    """Answers with whoever the turn is running as."""

    async def generate(self, messages: list[dict[str, Any]], *, tools: Any = None, **kwargs: object) -> Any:
        from app.llm.base import LLMResponse

        return LLMResponse(text=f"You are {get_current_user_id()}.")

    async def aclose(self) -> None:
        pass


def _converse(client: TestClient, siris: SirisAI, headers: dict[str, str] | None = None) -> list[dict[str, Any]]:
    app.dependency_overrides[get_siris] = lambda: siris
    try:
        response = client.post(
            "/siris/voice/converse",
            data={"synthesize": "false"},
            files={"file": ("a.wav", b"RIFF", "audio/wav")},
            headers=headers or {},
        )
        return [json.loads(line) for line in response.text.splitlines() if line.strip()]
    finally:
        app.dependency_overrides.pop(get_siris, None)


def test_converse_runs_the_turn_as_the_recognised_speaker(client: TestClient) -> None:
    siris = SirisAI(settings=Settings(), llm=_EchoUserLLM())
    siris.stt = _FakeSTT()  # type: ignore[assignment]
    siris.identify_speaker = lambda audio: {"user_id": "carol", "score": 0.93, "scores": {"carol": 0.93}}  # type: ignore[method-assign]

    events = _converse(client, siris)
    assert events[0] == {"type": "speaker", "user_id": "carol", "score": 0.93, "role": "adult"}
    assert events[-1]["response"] == "You are carol."


def test_an_explicit_user_header_wins_over_voice_recognition(client: TestClient) -> None:
    siris = SirisAI(settings=Settings(), llm=_EchoUserLLM())
    siris.stt = _FakeSTT()  # type: ignore[assignment]
    siris.identify_speaker = lambda audio: {"user_id": "carol", "score": 0.93, "scores": {}}  # type: ignore[method-assign]

    events = _converse(client, siris, headers={"X-Siris-User": "dave"})
    assert all(e["type"] != "speaker" for e in events)
    assert events[-1]["response"] == "You are dave."


def test_no_confident_match_stays_the_default_user(client: TestClient) -> None:
    siris = SirisAI(settings=Settings(), llm=_EchoUserLLM())
    siris.stt = _FakeSTT()  # type: ignore[assignment]
    siris.identify_speaker = lambda audio: {"user_id": None, "score": 0.4, "scores": {}}  # type: ignore[method-assign]
    events = _converse(client, siris)
    assert events[-1]["response"] == "You are default."


def test_context_says_who_is_talking() -> None:
    from app.core.user_context import set_current_user_id

    siris = SirisAI(settings=Settings())
    set_current_user_id("erin")
    try:
        assert "You are talking with: erin" in (siris.context.build(siris, "hi") or "")
    finally:
        set_current_user_id("default")
    assert "talking with" not in (siris.context.build(siris, "hi") or "")
