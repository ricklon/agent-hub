"""MCP tool bridge for page agents (browser-hosted MCP servers).

A page agent is a browser page that exposes tools (talking via Web Speech,
seeing via getUserMedia, discussion, site, agent status) and is driven by the
hub the same way a xiaozhi device is. The device acts as the MCP *server*;
the hub is the client.

A browser page cannot accept inbound HTTP, so the standard streamable-HTTP
transport does not fit. Instead this bridge uses a half-duplex adaptation:

  - GET  /mcp/v1/events?device_id=…&token=…   (SSE)  hub → page: JSON-RPC
    requests (tools/call) are pushed as `data:` events.
  - POST /mcp/v1/respond                       page → hub: a JSON-RPC response
    ({jsonrpc, id, result|error}) resolving a pending call.

The initialize / tools/list handshake is folded into page-agent registration
(``server.page_agent``), which stores the page's tool definitions here. This
module owns the live per-page-agent state; the durable registry row lives in
``registry/store``.

JSON-RPC shape mirrors the xiaozhi firmware MCP server (protocolVersion
2024-11-05): a tools/call result is ``{content:[{type:"text"|"image",…}],
isError:false}``.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger

from agent_hub.config import Settings
from agent_hub.registry.store import RegistryStore

_TAG = "mcp_bridge"

_MCP_PROTOCOL_VERSION = "2024-11-05"


@dataclass
class PageAgent:
    """Live state for one connected page agent."""

    device_id: str
    token: str
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    outbound: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)
    pending: dict[int, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    connected: bool = False
    last_seen: float = field(default_factory=time.monotonic)
    _next_id: int = 3  # ids 1/2 reserved for the folded initialize/tools/list

    def next_id(self) -> int:
        nid = self._next_id
        self._next_id += 1
        return nid


_page_agents: dict[str, PageAgent] = {}


def register_page_agent(
    device_id: str,
    token: str,
    tools: list[dict[str, Any]],
) -> PageAgent:
    """Create or replace the live handle for a page agent.

    Re-registration drops any stale pending calls from a previous socket so a
    reconnecting page does not inherit a dangling future.

    Args:
        device_id: Registry device id of the page agent.
        token: Bearer token issued at registration (validated on every call).
        tools: Tool definitions from the page's tools/list, each
            ``{name, description, inputSchema}``.

    Returns:
        The new live handle.
    """
    old = _page_agents.get(device_id)
    if old is not None:
        for fut in old.pending.values():
            if not fut.done():
                fut.cancel()
    handle = PageAgent(device_id=device_id, token=token)
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        raw_schema = tool.get("inputSchema")
        schema: dict[str, Any] = raw_schema if isinstance(raw_schema, dict) else {}
        handle.tools[name] = {
            "description": str(tool.get("description") or ""),
            "inputSchema": {
                "type": schema.get("type", "object"),
                "properties": schema.get("properties", {}),
                "required": [s for s in schema.get("required", []) if isinstance(s, str)],
            },
        }
    _page_agents[device_id] = handle
    logger.bind(tag=_TAG).info(
        f"Registered page agent {device_id!r} with {len(handle.tools)} tools: {list(handle.tools)}"
    )
    return handle


def unregister_page_agent(device_id: str) -> None:
    """Drop a page agent's live handle (called on dashboard delete / shutdown)."""
    handle = _page_agents.pop(device_id, None)
    if handle is not None:
        for fut in handle.pending.values():
            if not fut.done():
                fut.cancel()


def get_page_agent(device_id: str) -> PageAgent | None:
    return _page_agents.get(device_id)


def list_page_tool_definitions(device_id: str) -> list[dict[str, Any]]:
    """Return a page agent's tools in OpenAI function-calling format."""
    handle = _page_agents.get(device_id)
    if handle is None:
        return []
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": data["description"],
                "parameters": {
                    "type": "object",
                    "properties": data["inputSchema"].get("properties", {}),
                    "required": data["inputSchema"].get("required", []),
                },
            },
        }
        for name, data in handle.tools.items()
    ]


def find_page_agent_for_tool(tool_name: str) -> PageAgent | None:
    """Return a connected page agent that exposes the given tool name.

    Preference order: a connected agent first, then the most recently seen
    registered agent. Returns None when no page agent exposes the tool.
    """
    connected = [h for h in _page_agents.values() if h.connected and tool_name in h.tools]
    if connected:
        return max(connected, key=lambda h: h.last_seen)
    candidates = [h for h in _page_agents.values() if tool_name in h.tools]
    if candidates:
        return max(candidates, key=lambda h: h.last_seen)
    return None


def _result_text(result: dict[str, Any]) -> str:
    """Extract user-facing text (or an image data URL) from a tools/call result."""
    content = result.get("content", [])
    if isinstance(content, list) and content and isinstance(content[0], dict):
        c = content[0]
        if c.get("type") == "image":
            data = c.get("data") or c.get("image", "")
            mime = c.get("mimeType", "image/jpeg")
            if isinstance(data, str) and data.startswith("data:"):
                return data
            if data:
                return f"data:{mime};base64,{data}"
            return "[image: no data]"
        return str(c.get("text", str(result)))
    return str(result)


async def call_page_tool(
    device_id: str,
    name: str,
    arguments: dict[str, Any],
    timeout: float = 30.0,
) -> str:
    """Invoke a tool on a page agent and return its text/image result.

    Enqueues a JSON-RPC tools/call onto the page agent's outbound queue (the
    SSE stream delivers it to the browser) and awaits the matching response
    posted back to /mcp/v1/respond.

    Args:
        device_id: Page agent to call.
        name: Tool name (e.g. ``page.audio_speaker.speak``).
        arguments: Tool arguments object.
        timeout: Seconds to wait for the page to respond.

    Returns:
        Text result, or an image data URL for image content.

    Raises:
        KeyError: The page agent is not registered.
        TimeoutError: The page did not respond in time.
        RuntimeError: The page reported a tool error.
    """
    handle = _page_agents.get(device_id)
    if handle is None:
        raise KeyError(f"page agent not registered: {device_id!r}")
    call_id = handle.next_id()
    fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    handle.pending[call_id] = fut
    request = {
        "jsonrpc": "2.0",
        "id": call_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    await handle.outbound.put(request)
    logger.bind(tag=_TAG).debug(
        f"→ page {device_id!r} tools/call {name!r} id={call_id} args={arguments}"
    )
    try:
        result = await asyncio.wait_for(fut, timeout=timeout)
    except TimeoutError:
        handle.pending.pop(call_id, None)
        raise TimeoutError(f"page tool {name!r} timed out after {timeout}s") from None
    if isinstance(result, dict) and result.get("isError"):
        raise RuntimeError(f"page tool error: {result}")
    return _result_text(result) if isinstance(result, dict) else str(result)


async def events_generator(handle: PageAgent) -> Any:
    """Yield SSE chunks for one page agent: JSON-RPC requests and pings.

    This is the body of the ``/mcp/v1/events`` StreamingResponse, factored out
    so it can be unit-tested without an HTTP socket (httpx + ASGITransport
    buffers streaming bodies, so the response is not readable until complete).

    Yields:
        ``"data: <json>\\n\\n"`` for each queued JSON-RPC request, and
        ``": ping\\n\\n"`` keep-alives when the queue is idle.
    """
    try:
        while True:
            try:
                msg = await asyncio.wait_for(handle.outbound.get(), timeout=15.0)
                yield "data: " + json.dumps(msg) + "\n\n"
            except TimeoutError:
                yield ": ping\n\n"
    except BaseException:
        # Cancellation (client disconnect) propagates here; let Starlette tear
        # the stream down. We do not poll request.is_disconnected() because
        # that call blocks on ASGI transports without a real socket, which
        # hangs both the page and the test client.
        raise
    finally:
        handle.connected = False
        logger.bind(tag=_TAG).info(f"page {handle.device_id!r} SSE disconnected")


def _make_router(store: RegistryStore, settings: Settings) -> APIRouter:
    """Build the page-agent MCP bridge router.

    Mounted on ``server.mcp_bridge_port`` (the dashboard port by default). The
    SSE and respond endpoints are token-authenticated against the registry's
    per-device websocket token, the same token used by xiaozhi heartbeats.
    """
    router = APIRouter()
    bridge_port = settings.server.mcp_bridge_port
    _cors = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "*",
    }

    async def _authorize(device_id: str, token: str) -> bool:
        if not device_id or not token:
            return False
        return await store.validate_websocket_token(device_id, token)

    @router.get("/mcp/v1/events")
    async def events(
        request: Request,
        device_id: str = Query(...),
        token: str = Query(...),
    ) -> StreamingResponse:
        """SSE stream of JSON-RPC requests bound for one page agent."""
        if not await _authorize(device_id, token):
            return StreamingResponse(
                iter(["data: " + json.dumps({"error": "unauthorized"}) + "\n\n"]),
                media_type="text/event-stream",
                status_code=401,
                headers=_cors,
            )
        handle = _page_agents.get(device_id)
        if handle is None:
            return StreamingResponse(
                iter(["data: " + json.dumps({"error": "not registered"}) + "\n\n"]),
                media_type="text/event-stream",
                status_code=404,
                headers=_cors,
            )
        handle.connected = True
        handle.last_seen = time.monotonic()
        logger.bind(tag=_TAG).info(f"page {device_id!r} SSE connected on :{bridge_port}")

        async def event_stream() -> Any:
            async for chunk in events_generator(handle):
                yield chunk

        return StreamingResponse(event_stream(), media_type="text/event-stream", headers=_cors)

    @router.post("/mcp/v1/respond")
    async def respond(request: Request) -> JSONResponse:
        """Accept a JSON-RPC response posted back by a page agent."""
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                {"ok": False, "message": "invalid json"}, status_code=400, headers=_cors
            )
        if not isinstance(payload, dict):
            return JSONResponse(
                {"ok": False, "message": "expected object"}, status_code=400, headers=_cors
            )
        device_id = str(payload.get("device_id") or "")
        token = str(payload.get("token") or "")
        if not await _authorize(device_id, token):
            return JSONResponse(
                {"ok": False, "message": "unauthorized"}, status_code=401, headers=_cors
            )
        handle = _page_agents.get(device_id)
        if handle is None:
            return JSONResponse(
                {"ok": False, "message": "not registered"}, status_code=404, headers=_cors
            )

        msg_id = payload.get("id")
        result = payload.get("result")
        error = payload.get("error")
        if not isinstance(msg_id, int):
            return JSONResponse(
                {"ok": False, "message": "missing id"}, status_code=400, headers=_cors
            )
        fut = handle.pending.pop(msg_id, None)
        if fut is None or fut.done():
            return JSONResponse(
                {"ok": False, "message": "unknown or stale id"}, status_code=404, headers=_cors
            )
        if error is not None:
            fut.set_exception(RuntimeError(f"page error: {error}"))
        elif isinstance(result, dict):
            fut.set_result(result)
        else:
            fut.set_result({"content": [{"type": "text", "text": str(result)}], "isError": False})
        logger.bind(tag=_TAG).debug(f"← page {device_id!r} response id={msg_id}")
        return JSONResponse({"ok": True}, headers=_cors)

    @router.options("/mcp/v1/respond")
    async def respond_preflight() -> JSONResponse:
        return JSONResponse({}, headers=_cors)

    return router


def make_router(store: RegistryStore, settings: Settings) -> APIRouter:
    """Build and return the page-agent MCP bridge router.

    Args:
        store: Registry store used to validate per-page-agent tokens.
        settings: Server settings (mcp_bridge_port read for logging only).

    Returns:
        FastAPI router exposing /mcp/v1/events (SSE) and /mcp/v1/respond.
    """
    return _make_router(store, settings)
