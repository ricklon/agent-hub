"""Robots and scripts pair a key with a code, then sign in with it.

This is the device-code flow (RFC 8628) from Step 3 of the pairing plan:

1. ``POST /agent/pair/start`` with an OpenSSH public key. The hub answers
   with a 6-digit code, which the robot prints.
2. A person types the code into **Add device** on the dashboard.
3. The robot polls ``POST /agent/pair/poll`` until it gets 200.
4. From then on ``POST /agent/session`` with a signature over
   ``agent_id``, a timestamp, and a nonce returns the same token and bridge
   URLs ``/agent/register`` does. Heartbeat, goodbye, and the MCP bridge are
   unchanged.

The robot never holds a secret the hub also holds, and nothing the hub stores
lets anyone sign in as it. ``scripts/agent_pair.py`` does all of this for a
robot builder.
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from loguru import logger

from agent_hub.config import Settings
from agent_hub.registry.models import AgentKind
from agent_hub.registry.pairing import PairingLimitError, PairingRegistry
from agent_hub.registry.store import RegistryStore
from agent_hub.server import mcp_bridge
from agent_hub.server.agent_keys import (
    SESSION_CLOCK_SKEW_S,
    InvalidAgentKey,
    NonceCache,
    parse_public_key,
    session_message,
    verify_signature,
)
from agent_hub.server.client_ip import parse_trusted_proxies, resolve_client_ip

_TAG = "agent_pairing"

_PAIRABLE_KINDS = {k.value: k for k in (AgentKind.MCP, AgentKind.VOICE, AgentKind.AG2)}


def session_body(agent_id: str, token: str, settings: Settings) -> dict[str, Any]:
    """The JSON a signed-in agent gets: its token and where to use it."""
    return {
        "ok": True,
        "agent_id": agent_id,
        "device_id": agent_id,
        "token": token,
        "mcp_event_url": "/mcp/v1/events",
        "mcp_respond_url": "/mcp/v1/respond",
        "heartbeat_url": "/agent/heartbeat",
        "goodbye_url": "/agent/goodbye",
        "heartbeat_interval_seconds": settings.server.heartbeat_interval_seconds,
    }


def _fail(status: int, message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "message": message}, status_code=status)


def make_router(store: RegistryStore, settings: Settings) -> APIRouter:
    """Build the pair/poll/session router (device port).

    Args:
        store: Registry store; pairing shares its database.
        settings: Server settings: enrollment token, trusted proxies.

    Returns:
        Router serving /agent/pair/start, /agent/pair/poll and /agent/session.
    """
    router = APIRouter()
    pairing = PairingRegistry(store.session_factory)
    nonces = NonceCache()
    enrollment_token = settings.server.enrollment_token
    trusted_proxies = parse_trusted_proxies(settings.server.trusted_proxies)

    async def _json_body(request: Request) -> dict[str, Any]:
        try:
            body = json.loads((await request.body()) or b"{}")
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    @router.post("/agent/pair/start")
    async def pair_start(request: Request) -> JSONResponse:
        """Ask to join with a public key. Returns the code a person must type."""
        payload = await _json_body(request)
        if enrollment_token:
            # Optional filter in front of pairing: without the token, strangers
            # can't even create an attempt. Having it never pairs anything.
            supplied = str(
                payload.get("enrollment_token") or request.headers.get("x-enrollment-token") or ""
            )
            if not (supplied and secrets.compare_digest(supplied, enrollment_token)):
                return _fail(401, "enrollment token required")
        try:
            key = parse_public_key(str(payload.get("public_key") or ""))
        except InvalidAgentKey as exc:
            return _fail(400, str(exc))
        kind = _PAIRABLE_KINDS.get(str(payload.get("kind") or AgentKind.MCP.value).lower())
        if kind is None:
            return _fail(400, f"kind must be one of {sorted(_PAIRABLE_KINDS)}")
        agent_id = str(payload.get("agent_id") or "").strip()[:64]
        agent_id = agent_id or f"{kind.value}-{secrets.token_hex(6)}"
        existing = await store.get_agent(agent_id)
        if existing is not None and existing.kind != kind.value:
            return _fail(409, f"{agent_id!r} is already a {existing.kind} agent")
        details = {
            k: str(payload[k]).strip()[:128]
            for k in ("label", "persona", "owner", "version")
            if payload.get(k)
        }
        ip = resolve_client_ip(
            socket_peer=request.client.host if request.client else "",
            forwarded_for=request.headers.get("x-forwarded-for", ""),
            trusted_proxies=trusted_proxies,
        )
        try:
            attempt = await pairing.start_attempt(
                agent_id=agent_id,
                kind=kind.value,
                key_type=key.key_type,
                public_key=key.public_key,
                details=details,
                ip_address=ip or None,
            )
        except PairingLimitError as exc:
            return _fail(429, str(exc))
        logger.bind(tag=_TAG).info(
            f"Pairing requested for {agent_id!r} ({key.fingerprint}) from {ip or 'unknown'}"
        )
        return JSONResponse(
            {
                "ok": True,
                "attempt_id": attempt.attempt_id,
                "agent_id": agent_id,
                "code": attempt.code,
                "fingerprint": key.fingerprint,
                "expires_in": int((attempt.expires_at - attempt.created_at).total_seconds()),
                "poll_url": "/agent/pair/poll",
            }
        )

    @router.post("/agent/pair/poll")
    async def pair_poll(request: Request) -> JSONResponse:
        """202 while waiting for a person, 200 once approved, 410 once expired."""
        payload = await _json_body(request)
        attempt = await pairing.get_attempt(str(payload.get("attempt_id") or ""))
        if attempt is None:
            return _fail(404, "unknown pairing attempt")
        if attempt.approved_at is not None:
            return JSONResponse({"ok": True, "status": "approved", "agent_id": attempt.agent_id})
        if attempt.expires_at <= _naive_utc_now():
            return _fail(410, "this code expired; start pairing again")
        return JSONResponse({"ok": True, "status": "waiting"}, status_code=202)

    @router.post("/agent/session")
    async def session(request: Request) -> JSONResponse:
        """Sign in with the paired key: a signature over agent_id, timestamp, nonce."""
        payload = await _json_body(request)
        agent_id = str(payload.get("agent_id") or "").strip()
        nonce = str(payload.get("nonce") or "")
        try:
            timestamp = int(payload.get("timestamp") or 0)
        except (TypeError, ValueError):
            timestamp = 0
        if not agent_id or not nonce or len(nonce) > 128:
            return _fail(400, "agent_id, timestamp, nonce and signature required")
        if abs(time.time() - timestamp) > SESSION_CLOCK_SKEW_S:
            return _fail(401, "timestamp too far from the hub's clock")
        key = await pairing.active_key(agent_id)
        message = session_message(agent_id, timestamp, nonce)
        if key is None or not verify_signature(
            key.public_key, message, str(payload.get("signature") or "")
        ):
            logger.bind(tag=_TAG).warning(f"Rejected key sign-in for {agent_id!r}")
            return _fail(401, "not paired, or the signature doesn't match")
        if not nonces.use(agent_id, nonce):
            return _fail(401, "nonce already used")

        raw_tools = payload.get("tools")
        tools: list[dict[str, Any]] = (
            [t for t in raw_tools if isinstance(t, dict)] if isinstance(raw_tools, list) else []
        )
        await store.get_or_create_agent(
            device_id=agent_id,
            ip_address=resolve_client_ip(
                socket_peer=request.client.host if request.client else "",
                forwarded_for=request.headers.get("x-forwarded-for", ""),
                trusted_proxies=trusted_proxies,
            )
            or None,
        )
        token = await store.issue_websocket_token(agent_id)
        mcp_bridge.register_page_agent(agent_id, token, tools)
        logger.bind(tag=_TAG).info(
            f"Agent {agent_id!r} signed in with {key.fingerprint} ({len(tools)} tools)"
        )
        return JSONResponse(session_body(agent_id, token, settings))

    return router


def _naive_utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
