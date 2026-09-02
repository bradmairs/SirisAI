"""FastAPI application entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app.api.routes import events, health, memory, proactive, routines, siris, voice, web
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.siris import get_siris
from app.db.base import Base
from app.db.session import engine

settings = get_settings()
configure_logging(settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Make sure the sqlite file's parent directory exists before SQLAlchemy
    # tries to open it.
    if settings.database_url.startswith("sqlite:///"):
        db_path = Path(settings.database_url.removeprefix("sqlite:///"))
        db_path.parent.mkdir(parents=True, exist_ok=True)

    Base.metadata.create_all(bind=engine)
    # Warm the shared SirisAI instance (and its tool registry) at boot
    # rather than on first request.
    siris = get_siris()

    yield

    await siris.aclose()


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, lifespan=lifespan)

    app.include_router(health.router)
    app.include_router(siris.router)
    app.include_router(memory.router)
    app.include_router(events.router)
    app.include_router(proactive.router)
    app.include_router(routines.router)
    app.include_router(voice.router)
    app.include_router(web.router)

    return app


app = create_app()
