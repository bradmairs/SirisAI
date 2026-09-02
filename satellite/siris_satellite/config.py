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
    silence_timeout_seconds: float = 1.5
    max_recording_seconds: float = 15.0

    # None = the system default input/output device. Run
    # `python -m sounddevice` to list device indices if you need to pick
    # a specific mic/speaker on a Pi with more than one audio device.
    input_device: int | None = None
    output_device: int | None = None
