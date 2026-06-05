"""Check-in endpoint: /checkin/ and /xiaozhi/ota/ (permanent alias).

Devices POST here on boot to receive:
  - The WebSocket URL for their voice session
  - Server timestamp and timezone offset
  - Firmware update URL if a newer build is available (not yet implemented)

Rule: no activation gate. First-contact devices are auto-provisioned to
the hub-default persona and are functional immediately.
"""

from __future__ import annotations

import secrets
import socket
from contextlib import suppress
from typing import Any

from fastapi import APIRouter
from fastapi.requests import Request
from fastapi.responses import JSONResponse, PlainTextResponse
from loguru import logger

from agent_hub.config import Settings
from agent_hub.registry.models import AgentKind
from agent_hub.registry.store import RegistryStore
from agent_hub.server.protocol import CheckinRequest, CheckinResponse

_TAG = "checkin"

_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}


def _local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            host = s.getsockname()[0]
            return str(host)
    except Exception:
        return "127.0.0.1"


def _ws_url(settings: Settings) -> str:
    if settings.server.websocket:
        return settings.server.websocket
    return f"ws://{_local_ip()}:{settings.server.ws_port}/xiaozhi/v1/"


def _image_url(settings: Settings) -> str:
    ws = _ws_url(settings)
    # Derive HTTP base from WebSocket URL (ws:// → http://, wss:// → https://)
    http_base = ws.replace("ws://", "http://").replace("wss://", "https://")
    # Strip path component and append image endpoint
    from urllib.parse import urlparse, urlunparse

    p = urlparse(http_base)
    return urlunparse(p._replace(path="/xiaozhi/v1/image/", query="", fragment=""))


def make_router(store: RegistryStore, settings: Settings) -> APIRouter:
    """Build and return the check-in APIRouter with injected dependencies.

    Args:
        store: Registry store used to persist device first-contact records.
        settings: Application settings for URL construction and timezone.

    Returns:
        FastAPI router exposing /checkin/ and /xiaozhi/ota/ on GET/POST/OPTIONS.
    """
    router = APIRouter()

    @router.get("/checkin/")
    @router.get("/xiaozhi/ota/")
    async def checkin_get(_request: Request) -> PlainTextResponse:
        """Health-check GET — returns the WebSocket URL in plain text."""
        return PlainTextResponse(
            f"Check-in endpoint OK. WebSocket: {_ws_url(settings)}",
            headers=_CORS_HEADERS,
        )

    @router.post("/checkin/")
    @router.post("/xiaozhi/ota/")
    async def checkin_post(request: Request) -> JSONResponse:
        """Handle a device check-in.

        Registers the device on first contact and returns the WebSocket URL.
        """
        raw_body = await request.body()
        body: dict[str, Any] = {}
        if raw_body:
            with suppress(Exception):
                body = await request.json()

        headers = dict(request.headers)
        client_host = request.client.host if request.client else ""

        try:
            req = CheckinRequest.from_http(
                headers,
                body,
                client_host,
                dict(request.query_params),
            )
        except ValueError as exc:
            logger.bind(tag=_TAG).warning(f"Bad check-in: {exc}")
            return JSONResponse(
                {"success": False, "message": str(exc)},
                status_code=400,
                headers=_CORS_HEADERS,
            )

        enrollment_token = str((settings.raw.get("server") or {}).get("enrollment_token", ""))
        if enrollment_token and not secrets.compare_digest(req.enrollment_token, enrollment_token):
            logger.bind(tag=_TAG).warning(
                f"Rejected check-in from {req.device_id!r}: invalid enrollment token"
            )
            return JSONResponse(
                {"success": False, "message": "invalid enrollment token"},
                status_code=401,
                headers=_CORS_HEADERS,
            )

        logger.bind(tag=_TAG).info(
            f"Check-in from {req.device_id!r} at {req.ip_address} (fw {req.application_version})"
        )

        await store.get_or_create_agent(
            device_id=req.device_id,
            kind=AgentKind.XIAOZHI,
            ip_address=req.ip_address,
            firmware_version=req.application_version,
        )
        websocket_token = (
            await store.issue_websocket_token(req.device_id) if enrollment_token else ""
        )

        image_token = (settings.raw.get("server") or {}).get("image_token", "")
        resp = CheckinResponse(
            websocket_url=_ws_url(settings),
            firmware_version=req.application_version,
            timezone_offset_minutes=settings.server.timezone_offset * 60,
            token=websocket_token,
            image_url=_image_url(settings),
            image_token=image_token,
        )
        return JSONResponse(resp.to_json(), headers=_CORS_HEADERS)

    @router.options("/checkin/")
    @router.options("/xiaozhi/ota/")
    async def checkin_options() -> JSONResponse:
        """CORS preflight handler."""
        return JSONResponse({}, headers=_CORS_HEADERS)

    return router
