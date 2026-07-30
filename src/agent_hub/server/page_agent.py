"""Page agent: a browser page that acts as a talking + seeing MCP agent.

The page is served from the dashboard port at ``/dashboard/page-agent``. On
load it POSTs its tool list to ``/page-agent/register``, which creates a
registry row (``AgentKind.PAGE``, auto-bound to the ``hub-default`` persona —
no activation gate, same rule as xiaozhi devices) and issues a token. The page
then opens the MCP bridge SSE channel (``server.mcp_bridge``) to receive
``tools/call`` requests and posts results back.

Mirrors the xiaozhi firmware tool surface where it makes sense on a page:
``page.audio_speaker.speak`` (Web Speech), ``page.camera.take_photo``
(getUserMedia), plus ``page.discussion.*``, ``page.site.get`` and
``page.agent.status``. When the experimental WebMCP imperative API
(``document.modelContext.registerTool``) is available, the same functions are
also exposed to browser agents — see
https://github.com/ricklon/webmcp-litert-pwa for the local-first pattern.
"""

from __future__ import annotations

import secrets
import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger

from agent_hub import skills as server_skills
from agent_hub.config import Settings
from agent_hub.providers.llm import get_provider
from agent_hub.registry.models import AgentKind
from agent_hub.registry.store import RegistryStore
from agent_hub.server import mcp_bridge
from agent_hub.server._page_html import PAGE_HTML as _PAGE_AGENT_HTML

_TAG = "page_agent"

_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}

_VALID_ACTIVITIES = {"idle", "listening", "thinking", "speaking", "paused"}


def _bridge_base(request: Request, settings: Settings) -> str:
    """Base URL for the MCP bridge, honoring a dedicated bridge port."""
    parsed = urlsplit(str(request.url))
    scheme = parsed.scheme or "http"
    host = parsed.hostname or settings.server.host or "127.0.0.1"
    return f"{scheme}://{host}:{settings.server.mcp_bridge_port}"


def _new_device_id() -> str:
    return "page-" + secrets.token_hex(8)


def make_router(store: RegistryStore, settings: Settings, config: dict[str, Any]) -> APIRouter:
    """Build the page-agent router (mounted on the dashboard port).

    Args:
        store: Registry store used to create the page-agent row and issue tokens.
        settings: Server settings for URL construction and heartbeat cadence.
        config: Raw config dict (for LLM provider instantiation).

    Returns:
        FastAPI router serving the page HTML plus register/heartbeat/ask endpoints.
    """
    router = APIRouter()

    @router.get("/dashboard/page-agent", response_class=HTMLResponse)
    async def page_agent_page() -> HTMLResponse:
        return HTMLResponse(_PAGE_AGENT_HTML)

    @router.options("/page-agent/register")
    async def register_preflight() -> JSONResponse:
        return JSONResponse({}, headers=_CORS)

    @router.post("/page-agent/register")
    async def register(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "message": "expected object"}, status_code=400)
        device_id = str(payload.get("device_id") or "").strip() or _new_device_id()
        label = str(payload.get("label") or "").strip() or None
        raw_tools = payload.get("tools") or []
        tools: list[dict[str, Any]] = (
            [t for t in raw_tools if isinstance(t, dict)] if isinstance(raw_tools, list) else []
        )

        client_host = request.client.host if request.client else None
        await store.get_or_create_agent(
            device_id=device_id,
            kind=AgentKind.PAGE,
            label=label,
            ip_address=client_host,
            firmware_version="page-1.0",
        )
        token = await store.issue_websocket_token(device_id)
        mcp_bridge.register_page_agent(device_id, token, tools)
        logger.bind(tag=_TAG).info(f"Page agent registered {device_id!r} ({label or 'unlabelled'})")

        base = _bridge_base(request, settings)
        return JSONResponse(
            {
                "ok": True,
                "device_id": device_id,
                "token": token,
                "mcp_event_url": f"{base}/mcp/v1/events",
                "mcp_respond_url": f"{base}/mcp/v1/respond",
                "heartbeat_url": "/page-agent/heartbeat",
                "heartbeat_interval_seconds": settings.server.heartbeat_interval_seconds,
            },
            headers=_CORS,
        )

    @router.post("/page-agent/heartbeat")
    async def heartbeat(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "message": "expected object"}, status_code=400)
        device_id = str(payload.get("device_id") or "").strip()
        token = str(payload.get("token") or "").strip()
        if not device_id or not token:
            return JSONResponse(
                {"ok": False, "message": "device_id and token required"}, status_code=400
            )
        activity = str(payload.get("activity") or "idle").strip().lower()
        if activity not in _VALID_ACTIVITIES:
            activity = "idle"
        raw_tools = payload.get("mcp_tools") or []
        mcp_tools: list[str] = (
            [t for t in raw_tools if isinstance(t, str)] if isinstance(raw_tools, list) else []
        )
        accepted = await store.record_authenticated_heartbeat(
            device_id, token, None, activity, mcp_tools
        )
        if not accepted:
            return JSONResponse({"ok": False, "message": "invalid token"}, status_code=401)
        return JSONResponse({"ok": True, "server_time": int(time.time() * 1000)}, headers=_CORS)

    @router.options("/page-agent/heartbeat")
    async def heartbeat_preflight() -> JSONResponse:
        return JSONResponse({}, headers=_CORS)

    @router.options("/page-agent/ask")
    async def ask_preflight() -> JSONResponse:
        return JSONResponse({}, headers=_CORS)

    @router.post("/page-agent/ask")
    async def ask(request: Request) -> JSONResponse:
        """Run a text LLM turn for a page agent, routing tool calls to the page.

        Mirrors the voice session's LLM loop: collects the page's MCP tools plus
        server skills, calls the LLM with function-calling, and when the LLM
        calls a page tool (e.g. page.camera.take_photo) routes it through the
        MCP bridge. Returns the final text reply; the page speaks it locally.
        """
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                {"ok": False, "message": "expected object"}, status_code=400, headers=_CORS
            )
        if not isinstance(payload, dict):
            return JSONResponse(
                {"ok": False, "message": "expected object"}, status_code=400, headers=_CORS
            )
        device_id = str(payload.get("device_id") or "").strip()
        token = str(payload.get("token") or "").strip()
        text = str(payload.get("text") or "").strip()
        if not device_id or not token:
            return JSONResponse(
                {"ok": False, "message": "device_id and token required"},
                status_code=400,
                headers=_CORS,
            )
        if not text:
            return JSONResponse(
                {"ok": False, "message": "text required"}, status_code=400, headers=_CORS
            )
        if not await store.validate_websocket_token(device_id, token):
            return JSONResponse(
                {"ok": False, "message": "invalid token"}, status_code=401, headers=_CORS
            )

        persona = await store.get_persona_for_device(device_id)
        if persona is None:
            return JSONResponse(
                {"ok": False, "message": "no persona assigned"}, status_code=500, headers=_CORS
            )

        # Collect tools: page MCP tools + server skills (skip page_speak/page_see
        # which are wrappers for other agents — the page's own LLM calls its
        # tools directly via the bridge).
        page_tool_defs = mcp_bridge.list_page_tool_definitions(device_id)
        skill_defs = [
            d
            for d in server_skills.get_definitions()
            if d["function"]["name"] not in {"page_speak", "page_see"}
        ]
        tools = page_tool_defs + skill_defs

        # Load conversation history.
        history = await store.load_history(device_id, limit=persona.memory_window * 2)
        history.append({"role": "user", "content": text})

        # Build system prompt with tool descriptions (same pattern as ws_session).
        tool_lines: list[str] = []
        for d in tools:
            fn = d["function"]
            extra = ""
            if "camera" in fn["name"] or "photo" in fn["name"]:
                extra = " Always pass a 'question' arg describing what to look for."
            tool_lines.append(f"- {fn['name']}: {fn['description']}{extra}")
        system_prompt = persona.system_prompt or ""
        if tool_lines:
            system_prompt = (
                f"{system_prompt}\n\nAvailable tools you MUST use when relevant:\n"
                + "\n".join(tool_lines)
            ).strip()

        # Tool executor: route page tools via the bridge, skills via skills.run_result.
        page_tool_names = {d["function"]["name"] for d in page_tool_defs}

        async def _exec_tool(name: str, args: dict[str, Any]) -> str:
            if name in page_tool_names:
                timeout = 60.0 if ("camera" in name or "photo" in name) else 30.0
                return await mcp_bridge.call_page_tool(device_id, name, args, timeout=timeout)
            if server_skills.has_skill(name):
                result = await server_skills.run_result(name, args)
                return result.text
            return f"unknown tool: {name!r}"

        # Run the LLM turn (non-streaming — the page displays the full text).
        llm = get_provider(persona.llm_provider, config, model_override=persona.llm_model or None)
        try:
            reply = await llm.complete_with_tools(
                history, tools, _exec_tool, system_prompt=system_prompt
            )
        except Exception as exc:
            logger.bind(tag=_TAG).error(f"Page agent LLM turn failed: {exc}")
            return JSONResponse(
                {"ok": False, "message": f"LLM error: {exc}"}, status_code=500, headers=_CORS
            )

        reply = (reply or "").strip()
        if reply:
            await store.append_history(device_id, "user", text)
            await store.append_history(device_id, "assistant", reply)

        logger.bind(tag=_TAG).info(f"Page agent {device_id!r} ask: {text!r} → {reply[:80]!r}")
        return JSONResponse({"ok": True, "reply": reply}, headers=_CORS)

    return router
