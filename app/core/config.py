"""Application configuration.

All runtime configuration is centralized here and sourced from environment
variables (or a `.env` file). Nothing else in the app should call
`os.environ` directly -- add new settings here instead.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SIRISAI_", extra="ignore")

    app_name: str = "SirisAI"
    environment: str = "development"
    log_level: str = "INFO"

    host: str = "0.0.0.0"
    port: int = 8000

    database_url: str = "sqlite:///./data/sirisai.db"

    # Bearer token required on /siris/* routes (Authorization: Bearer <key>).
    # None/empty = auth disabled -- fine for local-only dev, not for anything
    # reachable off localhost. /health is never gated, so infra probes work
    # regardless.
    api_key: str | None = None

    # LLM settings are model-agnostic at the app layer; a specific backend
    # is wired up behind app/llm/base.py.
    llm_provider: str = "none"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3"

    # Speech (app/voice/base.py). "openai_compatible" points at any local
    # server speaking OpenAI's audio API shape (whisper.cpp server,
    # speaches/faster-whisper-server, openedai-speech, ...).
    stt_provider: str = "none"
    stt_base_url: str = "http://localhost:8081"
    stt_model: str = "whisper-1"

    tts_provider: str = "none"
    tts_base_url: str = "http://localhost:8082"
    tts_model: str = "tts-1"
    tts_voice: str = "default"


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor -- use as a FastAPI dependency."""
    return Settings()
