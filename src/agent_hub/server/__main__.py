"""Entry point: python -m agent_hub.server

Starts the unified FastAPI app (check-in + WebSocket + dashboard) under uvicorn.
All routes share a single process and SQLite registry, served on three ports:
  - ws_port (8000)      WebSocket voice sessions + image endpoint
  - http_port (8003)    Device check-in / OTA
  - dashboard_port (8001) Dashboard UI
"""

from __future__ import annotations

import asyncio
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from loguru import logger

from agent_hub.config import load_config, load_settings
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


def build_app() -> FastAPI:
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
        asyncio.create_task(_prewarm_providers(raw_config))

    @app.get("/")
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/dashboard/")

    app.include_router(make_checkin_router(store, settings))
    app.include_router(make_image_router(raw_config))
    app.include_router(make_ws_router(store, raw_config))
    app.include_router(make_dashboard_router(store, raw_config))

    return app


def _make_server(app: FastAPI, host: str, port: int) -> uvicorn.Server:
    cfg = uvicorn.Config(app, host=host, port=port, log_level="info")
    return uvicorn.Server(cfg)


if __name__ == "__main__":
    settings = load_settings()
    app = build_app()
    host = settings.server.host

    ports = sorted(
        {
            settings.server.ws_port,
            settings.server.http_port,
            settings.server.dashboard_port,
        }
    )

    servers = [_make_server(app, host, p) for p in ports]

    async def _serve() -> None:
        # Suppress the "Started server process" duplicate lines from each server
        # by sharing a single lifespan; uvicorn handles startup hooks on first serve.
        await asyncio.gather(*(s.serve() for s in servers))

    logger.info(f"Binding on ports {ports}")
    asyncio.run(_serve())
