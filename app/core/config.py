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

    # LLM settings are model-agnostic at the app layer; a specific backend
    # (e.g. Ollama) is wired up behind app/llm/base.py in a later milestone.
    llm_provider: str = "none"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3"


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor -- use as a FastAPI dependency."""
    return Settings()
