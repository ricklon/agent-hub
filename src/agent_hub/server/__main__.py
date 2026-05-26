"""Entry point: python -m agent_hub.server

Starts the unified FastAPI app (check-in + WebSocket + dashboard) under uvicorn.
All routes share a single process and SQLite registry.
"""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from loguru import logger

from agent_hub.config import load_config, load_settings
from agent_hub.dashboard.app import make_router as make_dashboard_router
from agent_hub.registry.store import RegistryStore
from agent_hub.server.checkin import make_router as make_checkin_router
from agent_hub.server.ws_session import make_router as make_ws_router


def build_app() -> FastAPI:
    """Construct the FastAPI application with all routers mounted.

    Returns:
        Configured FastAPI instance ready for uvicorn.
    """
    raw_config = load_config()
    settings = load_settings()
    store = RegistryStore(settings.registry.db_path)

    app = FastAPI(title="agent-hub", version="0.1.0")

    @app.on_event("startup")
    async def _startup() -> None:
        await store.initialize()
        logger.info(
            f"agent-hub ready — "
            f"check-in on :{settings.server.http_port}, "
            f"WS on :{settings.server.ws_port}, "
            f"dashboard on :{settings.server.dashboard_port}"
        )

    @app.get("/")
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/dashboard/")

    app.include_router(make_checkin_router(store, settings))
    app.include_router(make_ws_router(store, raw_config))
    app.include_router(make_dashboard_router(store, raw_config))

    return app


if __name__ == "__main__":
    settings = load_settings()
    uvicorn.run(
        "agent_hub.server.__main__:build_app",
        factory=True,
        host=settings.server.host,
        port=settings.server.ws_port,
        log_level="info",
    )
