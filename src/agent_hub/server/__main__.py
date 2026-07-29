"""Entry point: python -m agent_hub.server

Starts the unified FastAPI app (check-in + WebSocket + dashboard) under uvicorn.
All routes share a single process and SQLite registry, served on three ports:
  - ws_port (8000)      WebSocket voice sessions + image endpoint
  - http_port (8003)    Device check-in / OTA
  - dashboard_port (8001) Dashboard UI
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import uvicorn
from fastapi import APIRouter, FastAPI
from fastapi.responses import RedirectResponse
from loguru import logger

from agent_hub.config import Settings, load_config, load_settings
from agent_hub.dashboard.app import make_router as make_dashboard_router
from agent_hub.registry.store import RegistryStore
from agent_hub.server.checkin import make_router as make_checkin_router
from agent_hub.server.image_explain import make_router as make_image_router
from agent_hub.server.ws_session import make_router as make_ws_router

_prewarmed = False


async def _prewarm_providers(config: dict[str, Any]) -> None:
    """Load local ML models before the first voice turn so latency is consistent.

    Guarded by a module-level flag because the startup event fires once per
    uvicorn server instance (three times in the multi-port setup).
    """
    global _prewarmed
    if _prewarmed:
        return
    _prewarmed = True

    from pathlib import Path

    from agent_hub.providers.asr import get_provider as get_asr
    from agent_hub.server.audio import pcm_to_wav

    model_dir = config.get("asr", {}).get("funasr", {}).get("model_dir", "models/SenseVoiceSmall")
    if not Path(model_dir).exists():
        return  # funasr not installed/configured, nothing to warm

    try:
        logger.info(f"Pre-warming FunASR model ({model_dir})…")
        asr = get_asr("funasr", config)
        # 0.1 s of silence at 16 kHz (16-bit PCM) — just enough to trigger model load
        silent_wav = pcm_to_wav(bytes(3200), 16000)
        await asr.transcribe(silent_wav)
        logger.info("FunASR model warm — first turn will not stall.")
    except Exception as exc:
        logger.warning(f"ASR pre-warm failed (non-fatal): {exc}")


def _new_app(store: RegistryStore, settings: Settings, raw_config: dict[str, Any]) -> FastAPI:
    """Create an app shell with the shared startup hook attached."""
    app = FastAPI(title="agent-hub", version="0.1.0")

    @app.on_event("startup")
    async def _startup() -> None:
        # Safe to run per app: create_all is idempotent, the migration
        # statements are suppressed on re-run, and the default persona is
        # only seeded when absent.
        await store.initialize()
        logger.info(
            f"agent-hub ready — "
            f"check-in on :{settings.server.http_port}, "
            f"WS on :{settings.server.ws_port}, "
            f"dashboard on :{settings.server.dashboard_port}"
        )
        app.state.prewarm_task = asyncio.create_task(_prewarm_providers(raw_config))

    return app


def _add_dashboard_root(app: FastAPI) -> None:
    """Mount the `/` → `/dashboard/` redirect on the dashboard app only."""

    @app.get("/")
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/dashboard/")


def build_apps() -> dict[int, FastAPI]:
    """Build one app per configured port, mounting only that port's routes.

    Ports are a trust boundary. The dashboard can change personas, drive device
    tools, and read every transcript, so it must not answer on the device-facing
    ports an operator opens to the LAN for check-in and voice sessions.

    When two ports are configured to the same value their apps are merged, so
    single-port setups keep every route rather than silently losing one.

    Returns:
        Mapping of port number to the app that should be bound to it.
    """
    raw_config = load_config()
    settings = load_settings()
    store = RegistryStore(settings.registry.db_path)

    groups: list[tuple[int, list[APIRouter], bool]] = [
        (
            settings.server.ws_port,
            [make_ws_router(store, raw_config), make_image_router(raw_config)],
            False,
        ),
        (settings.server.http_port, [make_checkin_router(store, settings)], False),
        (settings.server.dashboard_port, [make_dashboard_router(store, raw_config)], True),
    ]

    apps: dict[int, FastAPI] = {}
    for port, routers, is_dashboard in groups:
        app = apps.get(port)
        if app is None:
            app = _new_app(store, settings, raw_config)
            apps[port] = app
        for router in routers:
            app.include_router(router)
        if is_dashboard:
            _add_dashboard_root(app)

    dashboard_port = settings.server.dashboard_port
    if dashboard_port in {settings.server.ws_port, settings.server.http_port}:
        logger.warning(
            f"Dashboard shares port {dashboard_port} with a device endpoint, so it is "
            f"reachable wherever devices can reach the hub. Give the dashboard its own "
            f"port, or set AGENT_HUB_SERVER_DASHBOARD_PASSWORD."
        )

    return apps


def build_app() -> FastAPI:
    """Build a single app with every route mounted, ignoring port separation.

    Retained for tests and for embedding agent-hub in another ASGI application.
    The production entrypoint uses build_apps() so that the dashboard does not
    answer on the device-facing ports.
    """
    raw_config = load_config()
    settings = load_settings()
    store = RegistryStore(settings.registry.db_path)

    app = _new_app(store, settings, raw_config)
    _add_dashboard_root(app)
    app.include_router(make_checkin_router(store, settings))
    app.include_router(make_image_router(raw_config))
    app.include_router(make_ws_router(store, raw_config))
    app.include_router(make_dashboard_router(store, raw_config))

    return app


def prewarm_task(app: FastAPI) -> asyncio.Task[None] | None:
    """Return the retained provider prewarm task when startup has scheduled it."""
    return cast(asyncio.Task[None] | None, getattr(app.state, "prewarm_task", None))


def _make_server(app: FastAPI, host: str, port: int) -> uvicorn.Server:
    cfg = uvicorn.Config(app, host=host, port=port, log_level="info")
    return uvicorn.Server(cfg)


if __name__ == "__main__":
    settings = load_settings()
    apps = build_apps()
    host = settings.server.host

    ports = sorted(apps)
    servers = [_make_server(apps[p], host, p) for p in ports]

    async def _serve() -> None:
        # Suppress the "Started server process" duplicate lines from each server
        # by sharing a single lifespan; uvicorn handles startup hooks on first serve.
        await asyncio.gather(*(s.serve() for s in servers))

    logger.info(f"Binding on ports {ports}")
    asyncio.run(_serve())
