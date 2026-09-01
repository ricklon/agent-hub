"""A protocol client for the page-agent test harness.

The page agent is already a hardware-free client that speaks the hub's real
protocols. This drives them from Python, in-process, against an ASGI app built
the same way ``tests/server/test_page_agent.py`` builds one:

- ``POST /page-agent/register`` — create an ``AgentKind.PAGE`` row + token, and
  declare the page's MCP tools.
- ``POST /page-agent/ask`` — run a full text LLM + tool turn.
- ``GET /mcp/v1/events`` (consumed here via ``mcp_bridge.events_generator``) and
  ``POST /mcp/v1/respond`` — the JSON-RPC bridge that carries ``tools/call``
  requests to the page and results back.

``ask()`` fires the request as a task and concurrently pumps the bridge:
``/page-agent/ask`` blocks inside the handler on each page-tool call until the
matching result is posted to ``/mcp/v1/respond``, so the pump has to run while
the request is in flight (same shape as
``test_mcp_bridge.test_call_page_tool_resolves_via_respond``).

Scope: this exercises the LLM + tool loop, page-tool routing through the
bridge, skill execution, history, and system-prompt assembly. It does **not**
cover the device Opus voice path in ``server/ws_session.py``. Server skills
(e.g. ``get_current_time``) run in process and are invisible to the bridge, so
``Turn.tool_calls`` records page-tool calls only — see the open question in
``docs/agent-test-harness.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_hub.config import Settings
from agent_hub.registry.store import RegistryStore
from agent_hub.server import mcp_bridge
from agent_hub.server.mcp_bridge import make_router as make_mcp_bridge_router
from agent_hub.server.page_agent import make_router as make_page_agent_router

# A page-tool handler: given the call arguments, return the result. A ``str`` is
# sent as the JSON-RPC result scalar (the bridge wraps it); a ``dict`` is sent
# as the result object (e.g. ``{"content": [{"type": "image", ...}]}``). Raising
# reports the tool as errored. May be sync or async.
ToolHandler = Callable[[dict[str, Any]], Any]


class PageAgentError(RuntimeError):
    """A page-agent endpoint returned a non-success response."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class ToolCall:
    """One page-tool invocation observed on the bridge during a turn."""

    name: str
    arguments: dict[str, Any]
    duration_s: float = 0.0


@dataclass
class Turn:
    """The structured result of one :meth:`PageAgentClient.ask` call."""

    reply: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    def called(self, name: str) -> bool:
        """True when a page tool named ``name`` was invoked this turn."""
        return any(call.name == name for call in self.tool_calls)

    def call_args(self, name: str) -> dict[str, Any] | None:
        """Arguments of the first call to ``name``, or None if it was not called."""
        for call in self.tool_calls:
            if call.name == name:
                return call.arguments
        return None


class PageAgentClient:
    """Drives one synthetic page agent against an in-process hub."""

    def __init__(self, http: AsyncClient, store: RegistryStore) -> None:
        self._http = http
        self._store = store
        self.device_id: str = ""
        self.token: str = ""
        self._tool_specs: list[dict[str, Any]] = []
        self._handlers: dict[str, ToolHandler] = {}

    # ── construction ────────────────────────────────────────────────────────

    @classmethod
    @contextlib.asynccontextmanager
    async def session(
        cls,
        store: RegistryStore,
        *,
        settings: Settings | None = None,
        config: dict[str, Any] | None = None,
    ) -> AsyncIterator[PageAgentClient]:
        """Build an app with the page-agent + MCP-bridge routers and yield a client.

        The client is not yet registered — declare tools with :meth:`add_tool`,
        then call :meth:`register`. The page agent's bridge handle is dropped on
        exit.
        """
        settings = settings or Settings()
        app = FastAPI()
        app.include_router(make_page_agent_router(store, settings, config or {}))
        app.include_router(make_mcp_bridge_router(store, settings))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            client = cls(http, store)
            try:
                yield client
            finally:
                if client.device_id:
                    mcp_bridge.unregister_page_agent(client.device_id)

    # ── tool declaration ───────────────────────────────────────────────────

    def add_tool(
        self,
        name: str,
        description: str,
        handler: ToolHandler,
        *,
        properties: dict[str, Any] | None = None,
        required: list[str] | None = None,
    ) -> None:
        """Declare a page tool and its handler. Call before :meth:`register`."""
        self._tool_specs.append(
            {
                "name": name,
                "description": description,
                "inputSchema": {
                    "type": "object",
                    "properties": properties or {},
                    "required": required or [],
                },
            }
        )
        self._handlers[name] = handler

    def on_tool(self, name: str, handler: ToolHandler) -> None:
        """Set (or replace) the handler for an already-declared tool."""
        self._handlers[name] = handler

    # ── protocol ──────────────────────────────────────────────────────────

    async def register(self, *, label: str | None = None) -> None:
        """POST /page-agent/register with the declared tools; store the token."""
        payload: dict[str, Any] = {"tools": self._tool_specs}
        if self.device_id:
            payload["device_id"] = self.device_id
        if label:
            payload["label"] = label
        resp = await self._http.post("/page-agent/register", json=payload)
        body = _json_or_raise(resp, "register")
        if not body.get("ok"):
            raise PageAgentError(body.get("message", "register failed"), resp.status_code)
        self.device_id = str(body["device_id"])
        self.token = str(body["token"])

    async def ask(self, text: str, *, timeout: float = 30.0) -> Turn:
        """Run one text turn, pumping the bridge for page-tool calls it makes.

        Args:
            text: The user utterance.
            timeout: Seconds to wait for ``/page-agent/ask`` to return.

        Returns:
            The reply, any images, and the ordered page-tool calls observed.
        """
        if not self.device_id or not self.token:
            raise PageAgentError("register() must be called before ask()", 0)

        seen: list[ToolCall] = []
        pump = asyncio.create_task(self._pump_bridge(seen))
        started = time.perf_counter()
        try:
            resp = await asyncio.wait_for(
                self._http.post(
                    "/page-agent/ask",
                    json={"device_id": self.device_id, "token": self.token, "text": text},
                ),
                timeout=timeout,
            )
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
        elapsed = time.perf_counter() - started

        body = _json_or_raise(resp, "ask")
        if not body.get("ok"):
            raise PageAgentError(body.get("message", "ask failed"), resp.status_code)
        return Turn(
            reply=str(body.get("reply", "")),
            tool_calls=seen,
            images=list(body.get("images", [])),
            elapsed_s=elapsed,
        )

    # ── bridge pump ───────────────────────────────────────────────────────

    async def _pump_bridge(self, sink: list[ToolCall]) -> None:
        """Consume the SSE event stream and answer tools/call requests.

        Runs until cancelled (by :meth:`ask` once the request returns).
        """
        handle = mcp_bridge.get_page_agent(self.device_id)
        if handle is None:
            return
        handle.connected = True
        async for chunk in mcp_bridge.events_generator(handle):
            if not chunk.startswith("data: "):
                continue  # ": ping" keep-alive
            try:
                msg = json.loads(chunk[len("data: ") :].strip())
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict) or msg.get("method") != "tools/call":
                continue
            await self._answer_call(msg, sink)

    async def _answer_call(self, msg: dict[str, Any], sink: list[ToolCall]) -> None:
        params = msg.get("params") or {}
        name = str(params.get("name") or "")
        args = dict(params.get("arguments") or {})
        call = ToolCall(name=name, arguments=args)
        sink.append(call)

        handler = self._handlers.get(name)
        started = time.perf_counter()
        response: dict[str, Any] = {
            "device_id": self.device_id,
            "token": self.token,
            "id": msg.get("id"),
        }
        if handler is None:
            response["error"] = {"code": -32601, "message": f"no handler for {name!r}"}
        else:
            try:
                result = handler(args)
                if inspect.isawaitable(result):
                    result = await result
                response["result"] = result
            except Exception as exc:  # noqa: BLE001 - surface any handler failure as a tool error
                response["error"] = {"code": -32000, "message": str(exc)}
        call.duration_s = time.perf_counter() - started
        await self._http.post("/mcp/v1/respond", json=response)


def _json_or_raise(resp: Any, what: str) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError as exc:
        raise PageAgentError(
            f"{what}: non-JSON {resp.status_code} response: {resp.text[:200]!r}",
            resp.status_code,
        ) from exc
    if not isinstance(body, dict):
        raise PageAgentError(f"{what}: expected a JSON object, got {type(body).__name__}", 0)
    return body


__all__ = ["PageAgentClient", "PageAgentError", "ToolCall", "ToolHandler", "Turn"]
