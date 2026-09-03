"""Registration for agents that are neither xiaozhi firmware nor a browser page.

A robot on build night is a script on a Pi, a laptop, or a microcontroller.
It is not running xiaozhi firmware, so ``/checkin/`` does not fit, and it is
not a browser, so ``/page-agent/register`` does not either — that one lives
on the dashboard port behind Cloudflare Access, which a headless robot
cannot authenticate to.

This module is the third door: a robot POSTs its tool list to
``/agent/register`` on the **device** port (the one Caddy exposes publicly)
and gets back a token plus the MCP bridge URLs. From then on it is an
ordinary bridged agent — the hub calls its tools exactly the way it calls a
page agent's, and the dashboard drives and tests it the same way.

Auth mirrors check-in: when ``server.enrollment_token`` is set the robot must
present it to register, and a hub reachable from the internet should always
set one. When it is empty (a LAN class night) registration is open, which is
the same trade-off ``/checkin/`` already makes for devices.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from loguru import logger

from agent_hub.config import Settings
from agent_hub.registry.models import AgentKind
from agent_hub.registry.store import RegistryStore
from agent_hub.server import mcp_bridge, session_state
from agent_hub.server.client_ip import parse_trusted_proxies, resolve_client_ip

_TAG = "agent_api"

_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}

_VALID_ACTIVITIES = frozenset({"idle", "listening", "thinking", "speaking", "paused"})

# Kinds a self-registering agent may claim. A robot is an MCP agent; the
# other two exist so a software voice agent or an AG2 agent can use the same
# door. It may not claim to be xiaozhi firmware or a browser page, because
# those two have their own registration paths and their own guarantees.
_SELF_REGISTER_KINDS = {
    AgentKind.MCP.value: AgentKind.MCP,
    AgentKind.VOICE.value: AgentKind.VOICE,
    AgentKind.AG2.value: AgentKind.AG2,
}


def _new_agent_id(kind: str) -> str:
    return f"{kind}-{secrets.token_hex(6)}"


def make_router(store: RegistryStore, settings: Settings) -> APIRouter:
    """Build the generic agent-registration router (device port).

    Args:
        store: Registry store for the agent row and its token.
        settings: Server settings — enrollment token, heartbeat cadence,
            trusted proxies for client-IP resolution.

    Returns:
        Router serving /agent/register, /agent/heartbeat and /agent/goodbye.
    """
    router = APIRouter()
    enrollment_token = settings.server.enrollment_token
    trusted_proxies = parse_trusted_proxies(settings.server.trusted_proxies)

    def _authorized(payload: dict[str, Any], request: Request) -> bool:
        """True when registration may proceed (open hub, or correct token)."""
        if not enrollment_token:
            return True
        supplied = str(
            payload.get("enrollment_token")
            or request.headers.get("x-enrollment-token")
            or request.query_params.get("enrollment_token")
            or ""
        )
        return bool(supplied) and secrets.compare_digest(supplied, enrollment_token)

    async def _json_body(request: Request) -> dict[str, Any]:
        try:
            body = json.loads((await request.body()) or b"{}")
        except Exception:
            return {}
        return body if isinstance(body, dict) else {}

    @router.options("/agent/register")
    async def register_preflight() -> JSONResponse:
        return JSONResponse({}, headers=_CORS)

    @router.post("/agent/register")
    async def register(request: Request) -> JSONResponse:
        """Register a robot (or other non-browser agent) and its MCP tools.

        Body: ``{agent_id?, label?, owner?, kind?, persona?, tools[],
        enrollment_token?}``. Re-registering with the same ``agent_id`` keeps
        the row and its history and issues a fresh token, so a robot that
        reboots mid-build-night comes back as itself.
        """
        payload = await _json_body(request)
        if not _authorized(payload, request):
            logger.bind(tag=_TAG).warning("Agent registration refused: bad enrollment token")
            return JSONResponse(
                {"ok": False, "message": "enrollment token required"}, status_code=401
            )

        kind_raw = str(payload.get("kind") or AgentKind.MCP.value).strip().lower()
        kind = _SELF_REGISTER_KINDS.get(kind_raw)
        if kind is None:
            return JSONResponse(
                {
                    "ok": False,
                    "message": f"kind must be one of {sorted(_SELF_REGISTER_KINDS)}",
                },
                status_code=400,
            )

        agent_id = str(payload.get("agent_id") or payload.get("device_id") or "").strip()
        agent_id = agent_id or _new_agent_id(kind.value)
        label = str(payload.get("label") or "").strip() or None
        owner = str(payload.get("owner") or "").strip() or None
        raw_tools = payload.get("tools")
        tools: list[dict[str, Any]] = (
            [t for t in raw_tools if isinstance(t, dict)] if isinstance(raw_tools, list) else []
        )

        await store.get_or_create_agent(
            device_id=agent_id,
            kind=kind,
            label=label,
            ip_address=resolve_client_ip(
                socket_peer=request.client.host if request.client else "",
                forwarded_for=request.headers.get("x-forwarded-for", ""),
                trusted_proxies=trusted_proxies,
            )
            or None,
            firmware_version=str(payload.get("version") or "").strip() or None,
        )
        if owner:
            await store.set_agent_owner(agent_id, owner)

        persona = str(payload.get("persona") or "").strip()
        if persona:
            if await store.get_persona_by_name(persona):
                await store.assign_persona(agent_id, persona)
            else:
                logger.bind(tag=_TAG).warning(
                    f"Agent {agent_id!r} asked for unknown persona {persona!r}; "
                    f"kept its current one"
                )

        token = await store.issue_websocket_token(agent_id)
        mcp_bridge.register_page_agent(agent_id, token, tools)
        logger.bind(tag=_TAG).info(
            f"Registered {kind.value} agent {agent_id!r} "
            f"({label or 'unlabelled'}, owner={owner or 'none'}, {len(tools)} tools)"
        )
        return JSONResponse(
            {
                "ok": True,
                "agent_id": agent_id,
                # device_id is the same value under the name the rest of the
                # API uses; both are returned so a client can use either.
                "device_id": agent_id,
                "token": token,
                "mcp_event_url": "/mcp/v1/events",
                "mcp_respond_url": "/mcp/v1/respond",
                "heartbeat_url": "/agent/heartbeat",
                "goodbye_url": "/agent/goodbye",
                "heartbeat_interval_seconds": settings.server.heartbeat_interval_seconds,
            },
            headers=_CORS,
        )

    @router.options("/agent/heartbeat")
    async def heartbeat_preflight() -> JSONResponse:
        return JSONResponse({}, headers=_CORS)

    @router.post("/agent/heartbeat")
    async def heartbeat(request: Request) -> JSONResponse:
        """Liveness plus current activity and tool list, same as a page agent."""
        payload = await _json_body(request)
        agent_id = str(payload.get("agent_id") or payload.get("device_id") or "").strip()
        token = str(payload.get("token") or "").strip()
        if not agent_id or not token:
            return JSONResponse(
                {"ok": False, "message": "agent_id and token required"}, status_code=400
            )
        activity = str(payload.get("activity") or "idle").strip().lower()
        if activity not in _VALID_ACTIVITIES:
            activity = "idle"
        fault = str(payload.get("fault") or "").strip() or None
        raw_tools = payload.get("tools")
        tool_names: list[str] = (
            [t for t in raw_tools if isinstance(t, str)] if isinstance(raw_tools, list) else []
        )
        accepted = await store.record_authenticated_heartbeat(
            agent_id, token, fault, activity, tool_names
        )
        if not accepted:
            return JSONResponse({"ok": False, "message": "invalid token"}, status_code=401)
        return JSONResponse({"ok": True, "server_time": int(time.time() * 1000)}, headers=_CORS)

    @router.options("/agent/goodbye")
    async def goodbye_preflight() -> JSONResponse:
        return JSONResponse({}, headers=_CORS)

    @router.post("/agent/goodbye")
    async def goodbye(request: Request) -> JSONResponse:
        """Announce a clean shutdown so the dashboard shows offline immediately."""
        payload = await _json_body(request)
        agent_id = str(payload.get("agent_id") or payload.get("device_id") or "").strip()
        token = str(payload.get("token") or "").strip()
        if not await store.mark_agent_offline(agent_id, token):
            return JSONResponse({"ok": False, "message": "invalid token"}, status_code=401)
        mcp_bridge.unregister_page_agent(agent_id)
        session_state.set_pipeline_status(agent_id, "idle")
        logger.bind(tag=_TAG).info(f"Agent {agent_id!r} said goodbye")
        return JSONResponse({"ok": True}, headers=_CORS)

    return router
