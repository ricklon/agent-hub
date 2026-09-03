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
from starlette.middleware.trustedhost import TrustedHostMiddleware

from agent_hub import spend
from agent_hub.config import Settings, load_config, load_settings
from agent_hub.dashboard.app import make_router as make_dashboard_router
from agent_hub.dashboard.audit import DashboardAuditMiddleware
from agent_hub.dashboard.authorization import DashboardAuthorization
from agent_hub.registry.store import RegistryStore
from agent_hub.server.checkin import make_router as make_checkin_router
from agent_hub.server.heartbeat import make_router as make_heartbeat_router
from agent_hub.server.image_explain import make_router as make_image_router
from agent_hub.server.mcp_bridge import make_router as make_mcp_bridge_router
from agent_hub.server.page_agent import make_router as make_page_agent_router
from agent_hub.server.ws_session import make_router as make_ws_router

_prewarmed = False
_pruning = False


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


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def parse_allowed_hosts(raw: str) -> list[str]:
    """Split the comma-separated server.allowed_hosts setting into a list."""
    return [h.strip() for h in raw.split(",") if h.strip()]


def _warn_if_dashboard_exposed(settings: Settings) -> None:
    """Warn when the dashboard is reachable off-loopback with no authentication.

    Deliberately a warning rather than a hard failure: the zero-config
    classroom flow has to keep working. Anyone running this beyond their own
    machine needs to see it, though — an unauthenticated dashboard grants
    control of every device and read access to every transcript.
    """
    access_identity_configured = bool(
        settings.server.dashboard_access_team_domain and settings.server.dashboard_access_audience
    )
    if (
        settings.server.host in _LOOPBACK_HOSTS
        or settings.server.dashboard_password
        or access_identity_configured
    ):
        return

    logger.warning(
        f"Dashboard is bound to {settings.server.host}:{settings.server.dashboard_port} "
        f"with no authentication. Any host that can reach it can control every device "
        f"and read all transcripts. Set AGENT_HUB_SERVER_DASHBOARD_PASSWORD or the "
        f"Cloudflare Access identity settings, set AGENT_HUB_SERVER_ALLOWED_HOSTS to "
        f"block DNS-rebinding, or bind to 127.0.0.1."
    )


def _new_app(store: RegistryStore, settings: Settings, raw_config: dict[str, Any]) -> FastAPI:
    """Create an app shell with the shared startup hook attached."""
    app = FastAPI(title="agent-hub", version="0.1.0")

    @app.on_event("startup")
    async def _startup() -> None:
        # Safe to run per app: initialize() is guarded by a lock and a
        # done-flag, so only the first app to start does the work.
        await store.initialize()
        spend.configure(store, raw_config)
        logger.info(
            f"agent-hub ready — "
            f"check-in on :{settings.server.http_port}, "
            f"WS on :{settings.server.ws_port}, "
            f"dashboard on :{settings.server.dashboard_port}, "
            f"MCP bridge on :{settings.server.mcp_bridge_port}"
        )
        app.state.prewarm_task = asyncio.create_task(_prewarm_providers(raw_config))
        app.state.prune_task = asyncio.create_task(_prune_page_agents(store, raw_config))

    return app


async def _prune_page_agents(store: RegistryStore, config: dict[str, Any]) -> None:
    """Hourly sweep that removes page-agent rows left behind by closed tabs.

    Only page agents are removed automatically; devices are pruned solely
    from the dashboard, by a person. Guarded by a module-level flag like the
    prewarm, because startup fires once per uvicorn server instance (three
    times in the multi-port setup) and one sweeper is enough.
    """
    global _pruning
    if _pruning:
        return
    _pruning = True

    from agent_hub.dashboard.cleanup import PAGE_ONLY, StalePolicy, prune

    policy = StalePolicy.from_config(config)
    while True:
        try:
            await prune(store, policy, kinds=PAGE_ONLY)
        except Exception as exc:  # noqa: BLE001 - a sweep failure must not kill the loop
            logger.warning(f"page-agent prune failed: {exc}")
        await asyncio.sleep(3600)


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
    store = RegistryStore(
        settings.registry.db_path,
        default_asr_provider=str(
            (raw_config.get("asr") or {}).get("default_provider") or "funasr_onnx"
        ),
    )
    dashboard_auth = DashboardAuthorization(store, raw_config)

    groups: list[tuple[int, list[APIRouter], bool]] = [
        (
            settings.server.ws_port,
            [make_ws_router(store, raw_config), make_image_router(raw_config, store)],
            False,
        ),
        (
            settings.server.http_port,
            [make_checkin_router(store, settings), make_heartbeat_router(store, settings)],
            False,
        ),
        (
            settings.server.dashboard_port,
            [
                make_dashboard_router(store, raw_config, dashboard_auth),
                make_page_agent_router(store, settings, raw_config, dashboard_auth),
            ],
            True,
        ),
        (
            settings.server.mcp_bridge_port,
            [make_mcp_bridge_router(store, settings)],
            False,
        ),
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
            app.add_middleware(DashboardAuditMiddleware, store=store)

    dashboard_port = settings.server.dashboard_port
    shares_device_port = dashboard_port in {settings.server.ws_port, settings.server.http_port}
    if shares_device_port:
        logger.warning(
            f"Dashboard shares port {dashboard_port} with a device endpoint, so it is "
            f"reachable wherever devices can reach the hub. Give the dashboard its own "
            f"port, or set AGENT_HUB_SERVER_DASHBOARD_PASSWORD."
        )

    # Host allowlisting is the real DNS-rebinding defence: it rejects a forged
    # Host before routing. Applied to the dashboard app only — devices connect
    # by bare LAN IP, and an allowlist of hostnames would reject their check-in
    # outright, which fails in a way that looks like a network fault.
    allowed_hosts = parse_allowed_hosts(settings.server.allowed_hosts)
    if allowed_hosts:
        apps[dashboard_port].add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
        if shares_device_port:
            logger.warning(
                f"allowed_hosts is enforced on port {dashboard_port}, which also serves "
                f"device endpoints. Devices connecting by IP will be rejected unless that "
                f"IP is listed in AGENT_HUB_SERVER_ALLOWED_HOSTS."
            )

    _warn_if_dashboard_exposed(settings)

    return apps


def build_app() -> FastAPI:
    """Build a single app with every route mounted, ignoring port separation.

    Retained for tests and for embedding agent-hub in another ASGI application.
    The production entrypoint uses build_apps() so that the dashboard does not
    answer on the device-facing ports.
    """
    raw_config = load_config()
    settings = load_settings()
    store = RegistryStore(
        settings.registry.db_path,
        default_asr_provider=str(
            (raw_config.get("asr") or {}).get("default_provider") or "funasr_onnx"
        ),
    )
    dashboard_auth = DashboardAuthorization(store, raw_config)

    app = _new_app(store, settings, raw_config)
    _add_dashboard_root(app)
    app.include_router(make_checkin_router(store, settings))
    app.include_router(make_heartbeat_router(store, settings))
    app.include_router(make_image_router(raw_config, store))
    app.include_router(make_ws_router(store, raw_config))
    app.include_router(make_dashboard_router(store, raw_config, dashboard_auth))
    app.include_router(make_page_agent_router(store, settings, raw_config, dashboard_auth))
    app.include_router(make_mcp_bridge_router(store, settings))
    app.add_middleware(DashboardAuditMiddleware, store=store)

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
