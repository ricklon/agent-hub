"""Calling one tool on a xiaozhi device, whose MCP server rides in its WS session."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_hub.dashboard.app import make_router
from agent_hub.registry.models import AgentKind
from agent_hub.registry.store import RegistryStore
from agent_hub.server import session_state

DEVICE = "9c:9e:6e:f7:16:0c"


class FakeMcpClient:
    """Stands in for the WS session's MCP client."""

    def __init__(self, ready: bool = True) -> None:
        self.ready = ready
        self.tools: dict[str, dict[str, Any]] = {
            "self_coglet_state": {"description": "state", "inputSchema": {}},
            "self_coglet_gaze": {"description": "gaze", "inputSchema": {}},
        }
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, args: dict[str, Any], timeout: float = 30.0) -> str:
        self.calls.append((name, args))
        if name == "self_coglet_gaze":
            raise RuntimeError("MCP tool error: released")
        return '{"released":true}'


@pytest.fixture()
def device_client():
    client = FakeMcpClient()
    session_state.register_mcp_client(DEVICE, client)
    yield client
    session_state.unregister_session(DEVICE)


async def _client(store: RegistryStore) -> AsyncClient:
    app = FastAPI()
    app.include_router(make_router(store, {}))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_call_tool_json_reaches_a_device_session(
    store: RegistryStore, device_client: FakeMcpClient
) -> None:
    await store.get_or_create_agent(DEVICE, kind=AgentKind.XIAOZHI)
    async with await _client(store) as c:
        resp = await c.post(
            f"/dashboard/agents/{DEVICE}/call_tool.json",
            json={"tool": "self_coglet_state", "arguments": {}},
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "result": '{"released":true}'}
    assert device_client.calls == [("self_coglet_state", {})]


async def test_the_html_console_route_reaches_a_device_too(
    store: RegistryStore, device_client: FakeMcpClient
) -> None:
    await store.get_or_create_agent(DEVICE, kind=AgentKind.XIAOZHI)
    async with await _client(store) as c:
        resp = await c.post(
            f"/dashboard/agents/{DEVICE}/call_tool", data={"tool": "self_coglet_state"}
        )
    assert resp.status_code == 200
    assert "released" in resp.text


async def test_device_tool_errors_and_unknown_tools_are_reported(
    store: RegistryStore, device_client: FakeMcpClient
) -> None:
    await store.get_or_create_agent(DEVICE, kind=AgentKind.XIAOZHI)
    async with await _client(store) as c:
        failed = await c.post(
            f"/dashboard/agents/{DEVICE}/call_tool.json", json={"tool": "self_coglet_gaze"}
        )
        unknown = await c.post(
            f"/dashboard/agents/{DEVICE}/call_tool.json", json={"tool": "self_fly"}
        )
        bad = await c.post(
            f"/dashboard/agents/{DEVICE}/call_tool.json",
            json={"tool": "self_coglet_state", "arguments": [1]},
        )
    assert failed.status_code == 400 and "released" in failed.json()["error"]
    assert unknown.status_code == 400 and "does not expose" in unknown.json()["error"]
    assert bad.status_code == 400 and "JSON object" in bad.json()["error"]


async def test_a_device_that_has_not_listed_tools_yet_says_so(store: RegistryStore) -> None:
    await store.get_or_create_agent(DEVICE, kind=AgentKind.XIAOZHI)
    session_state.register_mcp_client(DEVICE, FakeMcpClient(ready=False))
    try:
        async with await _client(store) as c:
            resp = await c.post(
                f"/dashboard/agents/{DEVICE}/call_tool.json", json={"tool": "self_coglet_state"}
            )
    finally:
        session_state.unregister_session(DEVICE)
    assert resp.status_code == 400
    assert "has not listed its tools" in resp.json()["error"]


async def test_speak_json_and_inject_json_reach_a_device_session(store: RegistryStore) -> None:
    spoken: list[str] = []
    heard: list[str] = []

    async def speak(text: str) -> None:
        spoken.append(text)

    async def send_json(_msg: dict[str, Any]) -> None:
        return None

    async def inject(text: str) -> tuple[str, str | None]:
        heard.append(text)
        return "doing well", None

    await store.get_or_create_agent(DEVICE, kind=AgentKind.XIAOZHI)
    session_state.register_session(DEVICE, speak, send_json)
    session_state.register_injector(DEVICE, inject)
    try:
        async with await _client(store) as c:
            said = await c.post(f"/dashboard/agents/{DEVICE}/speak.json", json={"text": " hi "})
            asked = await c.post(
                f"/dashboard/agents/{DEVICE}/inject.json", json={"text": "how are you?"}
            )
            empty = await c.post(f"/dashboard/agents/{DEVICE}/speak.json", json={"text": "  "})
    finally:
        session_state.unregister_session(DEVICE)
    assert said.json() == {"ok": True} and spoken == ["hi"]
    assert asked.json() == {"ok": True, "reply": "doing well", "image": None}
    assert heard == ["how are you?"]
    assert empty.status_code == 400 and empty.json()["error"] == "text is required"


async def test_speak_json_and_inject_json_say_when_the_device_is_offline(
    store: RegistryStore,
) -> None:
    await store.get_or_create_agent(DEVICE, kind=AgentKind.XIAOZHI)
    async with await _client(store) as c:
        said = await c.post(f"/dashboard/agents/{DEVICE}/speak.json", json={"text": "hi"})
        asked = await c.post(f"/dashboard/agents/{DEVICE}/inject.json", json={"text": "hi"})
    assert said.status_code == 409 and asked.status_code == 409
    assert "not connected" in said.json()["error"]


# ── scripts/device_mcp_proxy.py ──────────────────────────────────────────────


def _load_proxy_module() -> Any:
    path = Path(__file__).resolve().parents[2] / "scripts" / "device_mcp_proxy.py"
    spec = importlib.util.spec_from_file_location("device_mcp_proxy", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeHub:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def status(self) -> dict[str, Any]:
        names = [
            "self_get_device_status",
            "self_coglet_state",
            "self_coglet_stop",
            "self_coglet_release",
            "self_coglet_gaze",
            "self_coglet_animate",
        ]
        return {"mcp": {"ready": True, "tools": [{"name": n, "inputSchema": {}} for n in names]}}

    def call(self, tool: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        self.calls.append(tool)
        return True, "ok"

    def speak(self, text: str) -> tuple[bool, str]:
        self.calls.append(f"speak:{text}")
        return True, "spoken"

    def ask(self, text: str) -> tuple[bool, str]:
        self.calls.append(f"ask:{text}")
        return True, "fine, thanks"


def test_proxy_exposes_only_non_moving_tools_by_default() -> None:
    mod = _load_proxy_module()
    hub = FakeHub()
    proxy = mod.Proxy(hub, set())
    listed = proxy.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in listed["result"]["tools"]}
    assert names == {
        "hub_speak",
        "hub_ask",
        "self_get_device_status",
        "self_coglet_state",
        "self_coglet_stop",
        "self_coglet_release",
    }
    # The allowlist holds on calls, not just the listing.
    refused = proxy.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "self_coglet_gaze", "arguments": {}},
        }
    )
    assert refused["result"]["isError"] is True
    assert hub.calls == []


def test_proxy_all_tools_and_initialize() -> None:
    mod = _load_proxy_module()
    proxy = mod.Proxy(FakeHub(), None)
    init = proxy.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["capabilities"] == {"tools": {}}
    assert proxy.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    listed = proxy.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert len(listed["result"]["tools"]) == 8
    called = proxy.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "self_coglet_gaze", "arguments": {"horizontal": 10}},
        }
    )
    assert json.dumps(called["result"]) == json.dumps({"content": [{"type": "text", "text": "ok"}]})


def test_proxy_speak_and_ask_go_to_the_hub_not_the_device() -> None:
    mod = _load_proxy_module()
    hub = FakeHub()
    proxy = mod.Proxy(hub, set())

    def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call"}
        msg["params"] = {"name": name, "arguments": arguments}
        return proxy.handle(msg)["result"]

    assert call("hub_speak", {"text": "hello"})["content"][0]["text"] == "spoken"
    assert call("hub_ask", {"text": "how are you?"})["content"][0]["text"] == "fine, thanks"
    assert call("hub_ask", {"text": " "})["isError"] is True
    assert hub.calls == ["speak:hello", "ask:how are you?"]


def test_an_explicit_tool_list_can_leave_out_the_conversation_tools() -> None:
    mod = _load_proxy_module()
    proxy = mod.Proxy(FakeHub(), {"self_coglet_state"})
    listed = proxy.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert [t["name"] for t in listed["result"]["tools"]] == ["self_coglet_state"]
