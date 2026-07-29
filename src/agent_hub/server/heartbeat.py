"""Authenticated device heartbeat endpoint."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from agent_hub.config import Settings
from agent_hub.registry.store import RegistryStore


def _bearer_token(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def make_router(store: RegistryStore, settings: Settings) -> APIRouter:
    """Build the authenticated `/xiaozhi/heartbeat/` router.

    Args:
        store: Registry used to authenticate and persist the heartbeat.
        settings: Server settings containing the requested heartbeat interval.

    Returns:
        Router serving the device heartbeat contract.
    """
    router = APIRouter()

    @router.post("/xiaozhi/heartbeat/")
    async def heartbeat(request: Request) -> JSONResponse:
        device_id = request.headers.get("device-id", "").strip()
        if not device_id:
            return JSONResponse(
                {"ok": False, "message": "missing device-id header"}, status_code=400
            )

        body: dict[str, Any]
        try:
            parsed = await request.json()
            body = parsed if isinstance(parsed, dict) else {}
        except Exception:
            body = {}

        health = str(body.get("health") or "healthy").strip().lower()
        if health not in {"healthy", "degraded"}:
            return JSONResponse(
                {"ok": False, "message": "health must be healthy or degraded"},
                status_code=400,
            )
        fault = str(body.get("fault") or "").strip()[:512] or None
        if health == "degraded" and fault is None:
            fault = "device reported degraded health"
        if health == "healthy":
            fault = None
        activity = str(body.get("activity") or "idle").strip().lower()
        if activity not in {"idle", "listening", "thinking", "speaking", "paused"}:
            return JSONResponse(
                {"ok": False, "message": "invalid activity"},
                status_code=400,
            )
        raw_tools = body.get("mcp_tools") or []
        if not isinstance(raw_tools, list) or len(raw_tools) > 64:
            return JSONResponse(
                {"ok": False, "message": "mcp_tools must be an array of at most 64 names"},
                status_code=400,
            )
        mcp_tools: list[str] = []
        for value in raw_tools:
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > 128:
                return JSONResponse(
                    {"ok": False, "message": "invalid MCP tool name"},
                    status_code=400,
                )
            name = value.strip()
            if name not in mcp_tools:
                mcp_tools.append(name)

        accepted = await store.record_authenticated_heartbeat(
            device_id,
            _bearer_token(request),
            fault,
            activity,
            mcp_tools,
        )
        if not accepted:
            return JSONResponse({"ok": False, "message": "invalid device token"}, status_code=401)

        return JSONResponse(
            {
                "ok": True,
                "server_time": int(time.time() * 1000),
                "next_heartbeat_seconds": settings.server.heartbeat_interval_seconds,
            }
        )

    return router
