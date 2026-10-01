"""Satellite configuration, sourced from environment variables
(SIRISAI_SATELLITE_* prefix) or a .env file -- mirrors the main app's
app/core/config.py pattern.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class SatelliteSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SIRISAI_SATELLITE_", extra="ignore")

    siris_base_url: str = "http://localhost:8000"
    siris_api_key: str = ""

    # One of openWakeWord's bundled models (e.g. "hey_jarvis", "alexa") or
    # a path to a custom .onnx/.tflite model -- see the openWakeWord docs
    # for training a custom wake word.
    wake_word_model: str = "hey_jarvis"
    wake_word_threshold: float = 0.5

    sample_rate: int = 16000
    silence_timeout_seconds: float = 1.2
    max_recording_seconds: float = 15.0
    # How long to wait for you to start talking after the wake word.
    speech_start_timeout_seconds: float = 5.0

    # "silero" (speech detection model bundled with openWakeWord -- ignores
    # fans, TVs and other steady noise far better) or "amplitude" (loudness
    # only). Falls back to amplitude if the Silero model isn't downloaded.
    vad: str = "silero"
    vad_threshold: float = 0.5

    # After a reply, keep listening this long for a follow-up without the
    # wake word (0 = only when SirisAI asked a question).
    follow_up_seconds: float = 6.0
    # Say the wake word while a reply is playing to cut it off and speak.
    barge_in: bool = True

    # Optional fixed identity for this satellite's requests (X-Siris-User),
    # e.g. a satellite in someone's own room. Leave empty to let SirisAI
    # recognise the speaker by voice when voiceprints are enrolled.
    user: str = ""

    # None = the system default input/output device. Run
    # `python -m sounddevice` to list device indices if you need to pick
    # a specific mic/speaker on a Pi with more than one audio device.
    input_device: int | None = None
    output_device: int | None = None

    # Room/presence-aware routing (milestone 48): which room this
    # satellite is in, and where SirisAI can reach it to push a reply
    # here even when a different satellite heard the request (see
    # inbound.py, SirisClient.register()). Empty room (default) = this
    # satellite never registers or opens the inbound listener at all --
    # existing pull-only behavior (chat() -> speak() -> play locally),
    # completely unaffected. callback_url must be reachable FROM the
    # SirisAI host, same "you configure it explicitly rather than SirisAI
    # trying to auto-detect it" reasoning as SIRISAI_PUBLIC_BASE_URL.
    room: str = ""
    callback_url: str = ""
    inbound_port: int = 8090
