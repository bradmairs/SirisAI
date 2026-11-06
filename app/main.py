"""FastAPI application entrypoint."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import psutil
from fastapi import FastAPI

from app.api.routes import (
    brain,
    career,
    cameras,
    cron,
    evals,
    health_data,
    hub,
    hud,
    events,
    health,
    integrations,
    learning,
    life,
    upkeep,
    memory,
    proactive,
    routines,
    satellites,
    servers,
    scenes,
    settings as settings_routes,
    siris,
    voice,
    web,
)
from app import __version__
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core import scheduler
from app.core.siris import get_siris
from app.db.base import Base
from app.db.migrate import apply_column_patches, apply_index_patches
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
    apply_column_patches(engine)
    apply_index_patches(engine)
    # Primes psutil's internal last-call state (a performance hardening
    # pass) so SystemInfoTool's non-blocking psutil.cpu_percent(interval=
    # None) -- called on nearly every chat turn via ContextEngine.build()
    # -- has something to diff against on its very first real call,
    # instead of returning a meaningless 0.0.
    psutil.cpu_percent()
    # Warm the shared SirisAI instance (and its tool registry) at boot
    # rather than on first request.
    siris = get_siris()

    scheduler_task = None
    if settings.scheduler_enabled:
        scheduler_task = asyncio.create_task(scheduler.run(siris, settings.scheduler_interval_seconds))

    yield

    if scheduler_task is not None:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass
    await siris.aclose()


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)

    app.include_router(health.router)
    app.include_router(siris.router)
    app.include_router(memory.router)
    app.include_router(events.router)
    app.include_router(proactive.router)
    app.include_router(learning.router)
    app.include_router(servers.router)
    app.include_router(settings_routes.router)
    app.include_router(career.router)
    app.include_router(upkeep.router)
    app.include_router(routines.router)
    app.include_router(scenes.router)
    app.include_router(cron.router)
    app.include_router(cameras.router)
    app.include_router(health_data.router)
    app.include_router(life.router)
    app.include_router(hud.router)
    app.include_router(hub.router)
    app.include_router(evals.router)
    app.include_router(satellites.router)
    app.include_router(integrations.router)
    app.include_router(voice.router)
    app.include_router(brain.router)
    app.include_router(web.router)

    return app


app = create_app()
