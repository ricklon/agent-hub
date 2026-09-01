"""Tests for persona-linked agent tools (borrowing another agent's MCP tools)."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_hub.config import Settings
from agent_hub.registry.models import Persona
from agent_hub.registry.store import RegistryStore
from agent_hub.server import mcp_bridge
from agent_hub.server.page_agent import (
    call_linked_tool,
    linked_tool_defs,
    resolve_linked_call,
)
from agent_hub.server.page_agent import make_router as make_page_agent_router
from tests.harness import ScriptedLLM, install_scripted_llm

_ROBOT_TOOLS = [
    {"name": "get_pose", "description": "current arm pose", "inputSchema": {}},
    {
        "name": "grip",
        "description": "close the gripper",
        "inputSchema": {},
        "annotations": {"destructiveHint": True},
    },
    {
        "name": "system_reboot",
        "description": "restart the controller",
        "inputSchema": {},
        # An explicit read-only hint overrides the risky-keyword match.
        "annotations": {"readOnlyHint": True},
    },
]


def _persona(linked: list[str]) -> Persona:
    return Persona(
        name="p",
        llm_provider="openai",
        tts_provider="edge",
        asr_provider="funasr_onnx",
        linked_agents=json.dumps(linked) if linked else None,
    )


@pytest.fixture
def robot():
    mcp_bridge.register_page_agent("robot-01", "tok", _ROBOT_TOOLS)
    handle = mcp_bridge.get_page_agent("robot-01")
    assert handle is not None
    handle.connected = True
    yield handle
    mcp_bridge.unregister_page_agent("robot-01")


def test_linked_tool_defs_namespaces_and_drops_destructive(robot) -> None:
    defs = linked_tool_defs(_persona(["robot-01"]))
    names = {d["function"]["name"] for d in defs}
    # grip is destructive → excluded; system_reboot is readOnlyHint → kept.
    assert names == {"robot-01.get_pose", "robot-01.system_reboot"}
    assert all(d["function"]["description"].startswith("[robot-01] ") for d in defs)


def test_linked_tool_defs_empty_without_a_link(robot) -> None:
    assert linked_tool_defs(_persona([])) == []


def test_linked_tool_defs_empty_when_agent_is_absent() -> None:
    assert linked_tool_defs(_persona(["ghost-99"])) == []


def test_resolve_linked_call_splits_on_the_agent_prefix() -> None:
    persona = _persona(["robot-01"])
    assert resolve_linked_call(persona, "robot-01.get_pose") == ("robot-01", "get_pose")
    assert resolve_linked_call(persona, "get_current_time") is None
    assert resolve_linked_call(_persona([]), "robot-01.get_pose") is None


async def test_call_linked_tool_when_offline_returns_a_clear_message() -> None:
    msg = await call_linked_tool("nobody-here", "grip", {})
    assert "not connected" in msg


async def _client(store: RegistryStore) -> AsyncClient:
    app = FastAPI()
    app.include_router(make_page_agent_router(store, Settings(), {}))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_ask_routes_a_borrowed_tool_call_to_the_linked_agent(
    store: RegistryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp_bridge.register_page_agent(
        "robot-b", "tok", [{"name": "get_pose", "description": "arm pose", "inputSchema": {}}]
    )
    handle = mcp_bridge.get_page_agent("robot-b")
    assert handle is not None
    handle.connected = True
    await store.update_persona("hub-default", linked_agents='["robot-b"]')

    llm = install_scripted_llm(
        monkeypatch,
        ScriptedLLM(tool_calls=[("robot-b.get_pose", {})], reply="The arm is home."),
    )

    async def _answer_robot() -> None:
        req = await asyncio.wait_for(handle.outbound.get(), timeout=3.0)
        fut = handle.pending.pop(req["id"])
        fut.set_result({"content": [{"type": "text", "text": "pose 0,0,0"}], "isError": False})

    responder = asyncio.create_task(_answer_robot())
    try:
        async with await _client(store) as client:
            reg = await client.post(
                "/page-agent/register", json={"device_id": "page-a", "tools": []}
            )
            token = reg.json()["token"]
            resp = await client.post(
                "/page-agent/ask",
                json={"device_id": "page-a", "token": token, "text": "where is the arm?"},
            )
        await asyncio.wait_for(responder, timeout=3.0)
    finally:
        responder.cancel()
        mcp_bridge.unregister_page_agent("robot-b")

    assert resp.status_code == 200
    assert resp.json()["reply"] == "The arm is home."
    # The borrowed tool ran on the robot and its result reached the model.
    assert llm.results == ["pose 0,0,0"]
