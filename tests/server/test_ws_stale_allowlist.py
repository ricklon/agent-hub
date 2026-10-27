"""A persona allowlist naming a tool the device no longer has gets logged.

Coglet v0.1.2 renamed ``self.coglet.builder`` to ``self.coglet.calibrate``. A
persona still listing ``self_coglet_builder`` allows nothing by that name and
can no longer be chosen by tool matching, silently, unless the hub says so.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from loguru import logger

from agent_hub.registry.store import RegistryStore
from agent_hub.server import session_state, ws_session

_real_sleep = asyncio.sleep  # the fixture below shortens asyncio.sleep globally
_COGLET_TOOLS = ["self.coglet.state", "self.coglet.stop", "self.coglet.calibrate"]


class _FakeCoglet:
    """Answers the hub's MCP initialize and tools/list like the firmware does."""

    def __init__(self, device_id: str) -> None:
        self.headers = {"device-id": device_id}
        self.query_params: dict[str, str] = {}
        self.state = SimpleNamespace()
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def accept(self) -> None:
        pass

    async def close(self, code: int = 1000, reason: str = "") -> None:
        pass

    async def receive_text(self) -> str:
        return json.dumps({"type": "hello", "version": 1, "features": {"mcp": True}})

    async def receive(self) -> dict[str, Any]:
        return await self.inbox.get()

    async def send_bytes(self, data: bytes) -> None:
        pass

    async def send_text(self, text: str) -> None:
        msg = json.loads(text)
        if msg.get("type") != "mcp":
            return
        request = msg["payload"]
        if request.get("method") == "initialize":
            result: dict[str, Any] = {"serverInfo": {"name": "coglet", "version": "0.1.2"}}
        elif request.get("method") == "tools/list":
            result = {"tools": [{"name": n, "inputSchema": {}} for n in _COGLET_TOOLS]}
        else:
            return
        reply = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        payload = json.dumps({"type": "mcp", "payload": reply})
        self.inbox.put_nowait({"type": "websocket.receive", "text": payload})


@pytest.fixture()
def warnings() -> Iterator[list[str]]:
    seen: list[str] = []
    sink = logger.add(lambda m: seen.append(str(m)), level="WARNING")
    yield seen
    logger.remove(sink)


@pytest.fixture(autouse=True)
def no_handshake_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    # MCPClient pauses 0.5 s between initialize and tools/list for real boards.
    async def short_sleep(delay: float, *args: Any) -> Any:
        return await _real_sleep(min(delay, 0.01), *args)

    monkeypatch.setattr("agent_hub.server.mcp_client.asyncio.sleep", short_sleep)


async def _run_session(store: RegistryStore, device_id: str) -> None:
    session_state.mark_greeted(device_id)
    ws = _FakeCoglet(device_id)
    endpoint = ws_session.make_router(store, {"server": {"keepalive_seconds": 0}}).routes[0]
    session = asyncio.create_task(endpoint.endpoint(ws))
    await _real_sleep(0.3)  # handshake and persona resolution
    ws.inbox.put_nowait({"type": "websocket.disconnect"})
    await asyncio.wait_for(session, timeout=5)


async def test_a_renamed_tool_in_the_persona_allowlist_is_logged(
    store: RegistryStore, warnings: list[str]
) -> None:
    device = "stale-allowlist"
    await store.get_or_create_agent(device)
    await store.create_persona("coglet-driver")
    stale = ["self_coglet_state", "self_coglet_stop", "self_coglet_builder"]
    await store.update_persona("coglet-driver", mcp_tools_allowlist=json.dumps(stale))
    await store.assign_persona(device, "coglet-driver")

    await _run_session(store, device)

    stale_warnings = [w for w in warnings if "doesn't list" in w]
    assert len(stale_warnings) == 1
    assert "'coglet-driver'" in stale_warnings[0]
    assert "['self_coglet_builder']" in stale_warnings[0]


async def test_a_current_allowlist_is_not_logged(store: RegistryStore, warnings: list[str]) -> None:
    device = "current-allowlist"
    await store.get_or_create_agent(device)
    await store.create_persona("coglet-driver")
    current = ["self_coglet_state", "self_coglet_stop", "self_coglet_calibrate"]
    await store.update_persona("coglet-driver", mcp_tools_allowlist=json.dumps(current))

    await _run_session(store, device)

    assert not [w for w in warnings if "doesn't list" in w]
    persona = await store.get_persona_for_device(device)
    assert persona is not None and persona.name == "coglet-driver"  # matched by tools
