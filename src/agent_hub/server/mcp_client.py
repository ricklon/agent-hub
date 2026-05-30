"""Device-side MCP client: discover and call tools exposed by xiaozhi firmware.

Protocol (over the shared WebSocket):
  Server → Device:  {"type": "mcp", "payload": <jsonrpc-request>}
  Device → Server:  {"type": "mcp", "payload": <jsonrpc-response>}

Handshake sequence:
  1. Server sends initialize (id=1) after hello
  2. Device replies → server sends tools/list (id=2)
  3. Device replies with tool definitions → client is ready
  4. Any tools/call (id≥3) can be sent at any time after that
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from fastapi import WebSocket
from loguru import logger

_TAG = "mcp_client"

_MCP_INIT_ID = 1
_MCP_LIST_ID = 2
_MCP_CALL_ID_START = 3


def _sanitize(name: str) -> str:
    return name.replace("-", "_").replace("/", "_").replace(".", "_")


class MCPClient:
    """Manages the MCP session for one device WebSocket connection."""

    def __init__(
        self,
        websocket: WebSocket,
        device_id: str,
        on_ready: Callable[[list[str]], None] | None = None,
    ) -> None:
        self._ws = websocket
        self._device_id = device_id
        self._on_ready = on_ready
        self.tools: dict[str, dict[str, Any]] = {}  # sanitized_name → tool_data
        self._name_map: dict[str, str] = {}  # sanitized → original
        self.ready = False
        self._next_id = _MCP_CALL_ID_START
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._id_lock = asyncio.Lock()

    def cancel_pending(self) -> None:
        """Cancel all pending tool call futures (call on disconnect)."""
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    # ── outbound helpers ─────────────────────────────────────────────────────

    async def _send(self, payload: dict[str, Any]) -> None:
        await self._ws.send_text(json.dumps({"type": "mcp", "payload": payload}))

    async def initialize(self, vision_url: str = "", vision_token: str = "") -> None:
        capabilities: dict[str, Any] = {}
        if vision_url:
            capabilities["vision"] = {"url": vision_url, "token": vision_token}

        logger.bind(tag=_TAG).debug(
            f"{self._device_id!r} sending MCP initialize (vision_url={vision_url!r})"
        )
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": _MCP_INIT_ID,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": capabilities,
                    "clientInfo": {"name": "agent-hub", "version": "0.1.0"},
                },
            }
        )

    async def _request_tools_list(self, cursor: str | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": _MCP_LIST_ID, "method": "tools/list"}
        if cursor:
            payload["params"] = {"cursor": cursor}
        await self._send(payload)

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        timeout: float = 30.0,
    ) -> str:
        """Call a device MCP tool and return the text result."""
        async with self._id_lock:
            call_id = self._next_id
            self._next_id += 1
            fut: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
            self._pending[call_id] = fut

        actual_name = self._name_map.get(tool_name, tool_name)
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": call_id,
                "method": "tools/call",
                "params": {"name": actual_name, "arguments": arguments},
            }
        )
        logger.bind(tag=_TAG).debug(
            f"{self._device_id!r} MCP call {actual_name!r} args={arguments}"
        )

        try:
            raw = await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError as exc:
            self._pending.pop(call_id, None)
            raise TimeoutError(f"MCP tool {tool_name!r} timed out after {timeout}s") from exc

        if isinstance(raw, dict):
            if raw.get("isError"):
                raise RuntimeError(f"MCP tool error: {raw}")
            content = raw.get("content", [])
            if content and isinstance(content[0], dict):
                c = content[0]
                if c.get("type") == "image":
                    # Build a data URL so the LLM provider can pass it as image_url
                    data = c.get("data") or c.get("image", "")
                    mime = c.get("mimeType", "image/jpeg")
                    if data:
                        if isinstance(data, str) and data.startswith("data:"):
                            return data
                        return f"data:{mime};base64,{data}"
                    return "[image: no data]"
                return str(c.get("text", str(raw)))
        return str(raw)

    # ── inbound dispatcher ───────────────────────────────────────────────────

    async def handle_message(self, payload: dict[str, Any]) -> None:
        """Dispatch an incoming MCP payload from the device."""
        if "result" in payload:
            msg_id = int(payload.get("id", 0))
            result = payload["result"]

            # Tool call response
            if msg_id in self._pending:
                self._pending.pop(msg_id).set_result(result)
                return

            if msg_id == _MCP_INIT_ID:
                info = result.get("serverInfo", {}) if isinstance(result, dict) else {}
                logger.bind(tag=_TAG).info(
                    f"{self._device_id!r} MCP server: {info.get('name')} {info.get('version')}"
                )
                await asyncio.sleep(0.5)
                await self._request_tools_list()

            elif msg_id == _MCP_LIST_ID:
                if not isinstance(result, dict):
                    return
                for tool in result.get("tools", []):
                    if not isinstance(tool, dict):
                        continue
                    orig_name = tool.get("name", "")
                    san_name = _sanitize(orig_name)
                    schema = (
                        tool.get("inputSchema", {})
                        if isinstance(tool.get("inputSchema"), dict)
                        else {}
                    )
                    self.tools[san_name] = {
                        "description": tool.get("description", ""),
                        "inputSchema": {
                            "type": schema.get("type", "object"),
                            "properties": schema.get("properties", {}),
                            "required": [
                                s for s in schema.get("required", []) if isinstance(s, str)
                            ],
                        },
                    }
                    self._name_map[san_name] = orig_name

                cursor = result.get("nextCursor") if isinstance(result, dict) else None
                if cursor:
                    await self._request_tools_list(cursor)
                else:
                    self.ready = True
                    tool_names = list(self.tools.keys())
                    logger.bind(tag=_TAG).info(
                        f"{self._device_id!r} MCP ready — {len(tool_names)} tools: {tool_names}"
                    )
                    if self._on_ready:
                        self._on_ready(tool_names)

        elif "error" in payload:
            msg_id = int(payload.get("id", 0))
            err = (
                payload.get("error", {}).get("message", "unknown")
                if isinstance(payload.get("error"), dict)
                else "unknown"
            )
            logger.bind(tag=_TAG).error(f"{self._device_id!r} MCP error id={msg_id}: {err}")
            if msg_id in self._pending:
                self._pending.pop(msg_id).set_exception(RuntimeError(f"MCP error: {err}"))

    # ── OpenAI tool definition format ─────────────────────────────────────────

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Return device tools in OpenAI function-calling format."""
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
            for name, data in self.tools.items()
        ]
