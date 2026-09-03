"""The dashboard console: call one tool by hand, ask a full turn, own an agent."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_hub.dashboard.app import make_router
from agent_hub.registry.models import AgentKind
from agent_hub.registry.store import RegistryStore
from agent_hub.server import mcp_bridge


async def _client(store: RegistryStore, config: dict[str, Any] | None = None) -> AsyncClient:
    app = FastAPI()
    app.include_router(make_router(store, config or {}))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _register_robot(device_id: str = "robot-01") -> None:
    """Put a connected robot with one tool on the bridge."""
    handle = mcp_bridge.register_page_agent(
        device_id,
        "tok",
        [
            {
                "name": "robot.drive",
                "description": "Drive forward.",
                "inputSchema": {"type": "object", "properties": {"speed": {"type": "integer"}}},
            }
        ],
    )
    handle.connected = True


async def _answer_one_call(device_id: str, reply: str) -> asyncio.Task[None]:
    """Play the robot: take the next queued call and post a result back."""

    async def pump() -> None:
        handle = mcp_bridge.get_page_agent(device_id)
        assert handle is not None
        request = await handle.outbound.get()
        fut = handle.pending.get(request["id"])
        if fut and not fut.done():
            fut.set_result({"content": [{"type": "text", "text": reply}], "isError": False})

    return asyncio.create_task(pump())


async def test_call_tool_runs_one_tool_and_shows_the_result(store: RegistryStore) -> None:
    await store.get_or_create_agent("robot-01", kind=AgentKind.MCP)
    _register_robot()
    try:
        async with await _client(store) as c:
            task = await _answer_one_call("robot-01", "drove at speed 5")
            resp = await c.post(
                "/dashboard/agents/robot-01/call_tool",
                data={"tool": "robot.drive", "arguments": json.dumps({"speed": 5})},
            )
            await task
    finally:
        mcp_bridge.unregister_page_agent("robot-01")
    assert resp.status_code == 200
    assert "drove at speed 5" in resp.text


async def test_call_tool_reports_bad_json_and_unknown_tools(store: RegistryStore) -> None:
    await store.get_or_create_agent("robot-01", kind=AgentKind.MCP)
    _register_robot()
    try:
        async with await _client(store) as c:
            bad_json = await c.post(
                "/dashboard/agents/robot-01/call_tool",
                data={"tool": "robot.drive", "arguments": "{speed: 5}"},
            )
            not_object = await c.post(
                "/dashboard/agents/robot-01/call_tool",
                data={"tool": "robot.drive", "arguments": "[1, 2]"},
            )
            unknown = await c.post(
                "/dashboard/agents/robot-01/call_tool",
                data={"tool": "robot.fly", "arguments": "{}"},
            )
    finally:
        mcp_bridge.unregister_page_agent("robot-01")
    assert bad_json.status_code == 400 and "must be JSON" in bad_json.text
    assert not_object.status_code == 400 and "JSON object" in not_object.text
    assert unknown.status_code == 400 and "does not expose a tool" in unknown.text


async def test_call_tool_says_so_when_the_robot_is_not_connected(store: RegistryStore) -> None:
    await store.get_or_create_agent("robot-02", kind=AgentKind.MCP)
    async with await _client(store) as c:
        resp = await c.post("/dashboard/agents/robot-02/call_tool", data={"tool": "robot.drive"})
    assert resp.status_code == 400
    assert "never registered" in resp.text


async def test_agent_page_shows_the_console_for_a_bridged_agent(store: RegistryStore) -> None:
    await store.get_or_create_agent("robot-01", kind=AgentKind.MCP, label="gripper")
    _register_robot()
    try:
        async with await _client(store) as c:
            page = await c.get("/dashboard/agents/robot-01")
    finally:
        mcp_bridge.unregister_page_agent("robot-01")
    assert "Tool console" in page.text
    assert "robot.drive" in page.text
    assert "/dashboard/agents/robot-01/call_tool" in page.text
    assert "Ask this agent" in page.text
    # A robot has no firmware to reboot.
    assert "Reboot device" not in page.text


async def test_a_device_page_has_no_tool_console(store: RegistryStore) -> None:
    await store.get_or_create_agent("df-k10", kind=AgentKind.XIAOZHI)
    async with await _client(store) as c:
        page = await c.get("/dashboard/agents/df-k10")
    assert "Tool console" not in page.text
    assert "Reboot device" in page.text


async def test_ask_runs_a_full_turn_through_the_shared_loop(
    store: RegistryStore, monkeypatch
) -> None:
    from agent_hub.server import agent_turn

    async def _fake_turn(_store, _config, device_id: str, text: str):
        return agent_turn.TurnResult(
            reply=f"heard {text} on {device_id}", tools_called=["robot.drive"]
        )

    monkeypatch.setattr("agent_hub.dashboard.app.run_turn", _fake_turn)
    await store.get_or_create_agent("robot-01", kind=AgentKind.MCP)
    async with await _client(store) as c:
        ok = await c.post("/dashboard/agents/robot-01/ask", data={"text": "go forward"})
        empty = await c.post("/dashboard/agents/robot-01/ask", data={"text": "  "})
    assert ok.status_code == 200
    assert "heard go forward on robot-01" in ok.text
    assert "robot.drive" in ok.text
    assert empty.status_code == 400


async def test_owner_can_be_set_and_filters_the_fleet_table(store: RegistryStore) -> None:
    await store.get_or_create_agent("robot-01", kind=AgentKind.MCP, label="rick bot")
    await store.get_or_create_agent("robot-02", kind=AgentKind.MCP, label="ada bot")
    async with await _client(store) as c:
        saved = await c.post("/dashboard/agents/robot-01/owner", data={"owner": "rick"})
        await c.post("/dashboard/agents/robot-02/owner", data={"owner": "ada"})
        everyone = await c.get("/dashboard/agents")
        just_rick = await c.get("/dashboard/agents", params={"owner": "rick"})
        home = await c.get("/dashboard/")
        cleared = await c.post("/dashboard/agents/robot-01/owner", data={"owner": ""})
        missing = await c.post("/dashboard/agents/nope/owner", data={"owner": "x"})
    assert saved.status_code == 200 and "rick" in saved.text
    assert "rick bot" in everyone.text and "ada bot" in everyone.text
    assert "rick bot" in just_rick.text and "ada bot" not in just_rick.text
    # The home page offers a chip per owner.
    assert "owner-chip" in home.text and "everyone" in home.text
    assert cleared.status_code == 200 and "cleared" in cleared.text
    assert missing.status_code == 404
    agent = await store.get_agent("robot-01")
    assert agent is not None and agent.owner is None


async def test_owners_survive_a_store_round_trip(store: RegistryStore) -> None:
    await store.get_or_create_agent("robot-01", kind=AgentKind.MCP)
    await store.get_or_create_agent("robot-02", kind=AgentKind.MCP)
    await store.set_agent_owner("robot-01", "rick")
    await store.set_agent_owner("robot-02", "ada")
    assert await store.list_owners() == ["ada", "rick"]
    # Over-long names are trimmed rather than rejected, so a paste cannot fail a save.
    await store.set_agent_owner("robot-01", "x" * 200)
    agent = await store.get_agent("robot-01")
    assert agent is not None and agent.owner is not None and len(agent.owner) == 64


def teardown_module() -> None:
    """Drop any bridge handles a failing test left behind."""
    for device_id in ("robot-01", "robot-02"):
        with contextlib.suppress(Exception):
            mcp_bridge.unregister_page_agent(device_id)


async def test_a_robot_page_hides_device_only_actions(store: RegistryStore) -> None:
    """Inject and Speak drive the device voice pipeline, which a robot has not got."""
    await store.get_or_create_agent("robot-01", kind=AgentKind.MCP)
    _register_robot()
    try:
        async with await _client(store) as c:
            page = await c.get("/dashboard/agents/robot-01")
            status = await c.get("/dashboard/agents/robot-01/status")
    finally:
        mcp_bridge.unregister_page_agent("robot-01")
    assert "Inject utterance" not in page.text
    assert "Send message to device" not in page.text
    assert "Ask this agent" in page.text
    # The connection table talks about the bridge, not firmware.
    assert "Bridge" in status.text
    assert "wake-word standby" not in status.text
    assert "robot.drive" in status.text
