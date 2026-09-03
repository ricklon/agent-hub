"""Robot (non-browser) agent registration on the device port."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_hub.config import Settings
from agent_hub.registry.models import AgentKind
from agent_hub.registry.store import RegistryStore
from agent_hub.server import mcp_bridge
from agent_hub.server.agent_api import make_router


def _settings(enrollment_token: str = "") -> Settings:
    return Settings.from_dict({"server": {"enrollment_token": enrollment_token}})


async def _client(store: RegistryStore, enrollment_token: str = "") -> AsyncClient:
    app = FastAPI()
    app.include_router(make_router(store, _settings(enrollment_token)))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


_TOOLS: list[dict[str, Any]] = [
    {
        "name": "robot.drive",
        "description": "Drive forward.",
        "inputSchema": {"type": "object", "properties": {"speed": {"type": "integer"}}},
        "annotations": {"destructiveHint": True},
    }
]


async def test_register_creates_an_mcp_agent_with_its_tools(store: RegistryStore) -> None:
    async with await _client(store) as c:
        resp = await c.post(
            "/agent/register",
            json={"agent_id": "robot-01", "label": "gripper", "owner": "rick", "tools": _TOOLS},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_id"] == "robot-01"
    assert data["token"]
    assert data["mcp_event_url"] == "/mcp/v1/events"

    agent = await store.get_agent("robot-01")
    assert agent is not None
    assert agent.kind == AgentKind.MCP.value
    assert agent.owner == "rick"
    assert agent.label == "gripper"
    # Tools reached the bridge, so the hub can already call them.
    handle = mcp_bridge.get_page_agent("robot-01")
    assert handle is not None
    assert "robot.drive" in handle.tools
    assert handle.tools["robot.drive"]["annotations"] == {"destructiveHint": True}


async def test_register_generates_an_id_and_defaults_to_mcp(store: RegistryStore) -> None:
    async with await _client(store) as c:
        resp = await c.post("/agent/register", json={"tools": []})
    agent_id = resp.json()["agent_id"]
    assert agent_id.startswith("mcp-")
    agent = await store.get_agent(agent_id)
    assert agent is not None and agent.kind == AgentKind.MCP.value


async def test_enrollment_token_is_required_when_configured(store: RegistryStore) -> None:
    async with await _client(store, enrollment_token="s3cret") as c:
        refused = await c.post("/agent/register", json={"agent_id": "robot-x", "tools": []})
        wrong = await c.post(
            "/agent/register", json={"agent_id": "robot-x", "enrollment_token": "nope"}
        )
        in_body = await c.post(
            "/agent/register", json={"agent_id": "robot-x", "enrollment_token": "s3cret"}
        )
        in_header = await c.post(
            "/agent/register",
            json={"agent_id": "robot-y"},
            headers={"X-Enrollment-Token": "s3cret"},
        )
    assert refused.status_code == 401
    assert wrong.status_code == 401
    assert in_body.status_code == 200
    assert in_header.status_code == 200
    assert await store.get_agent("robot-x") is not None


async def test_registration_is_open_when_no_token_is_configured(store: RegistryStore) -> None:
    """A LAN class night keeps the same no-activation-gate rule devices get."""
    async with await _client(store) as c:
        resp = await c.post("/agent/register", json={"agent_id": "robot-open"})
    assert resp.status_code == 200


async def test_a_robot_may_not_claim_to_be_firmware_or_a_page(store: RegistryStore) -> None:
    async with await _client(store) as c:
        xiaozhi = await c.post("/agent/register", json={"kind": "xiaozhi"})
        page = await c.post("/agent/register", json={"kind": "page"})
        voice = await c.post("/agent/register", json={"kind": "voice"})
    assert xiaozhi.status_code == 400
    assert page.status_code == 400
    assert voice.status_code == 200


async def test_register_binds_a_persona_and_ignores_an_unknown_one(store: RegistryStore) -> None:
    await store.create_persona(name="hero-robot", llm_provider="openai")
    async with await _client(store) as c:
        await c.post("/agent/register", json={"agent_id": "robot-p", "persona": "hero-robot"})
        persona = await store.get_persona_for_device("robot-p")
        assert persona is not None and persona.name == "hero-robot"

        await c.post("/agent/register", json={"agent_id": "robot-q", "persona": "ghost"})
    persona = await store.get_persona_for_device("robot-q")
    assert persona is not None and persona.name == "hub-default"


async def test_reregistering_keeps_the_row_and_issues_a_fresh_token(store: RegistryStore) -> None:
    """A robot that reboots mid-session comes back as itself, history intact."""
    async with await _client(store) as c:
        first = (await c.post("/agent/register", json={"agent_id": "robot-r"})).json()["token"]
        await store.append_history("robot-r", "user", "before the reboot")
        second = (await c.post("/agent/register", json={"agent_id": "robot-r"})).json()["token"]
    assert first != second
    assert await store.validate_websocket_token("robot-r", second) is True
    assert await store.validate_websocket_token("robot-r", first) is False
    assert len(await store.load_history("robot-r")) == 1


async def test_heartbeat_and_goodbye(store: RegistryStore) -> None:
    async with await _client(store) as c:
        token = (await c.post("/agent/register", json={"agent_id": "robot-h"})).json()["token"]
        good = await c.post(
            "/agent/heartbeat",
            json={
                "agent_id": "robot-h",
                "token": token,
                "activity": "thinking",
                "tools": ["robot.drive"],
                "fault": "low battery",
            },
        )
        bad = await c.post("/agent/heartbeat", json={"agent_id": "robot-h", "token": "nope"})
        bye = await c.post("/agent/goodbye", json={"agent_id": "robot-h", "token": token})
    assert good.status_code == 200
    assert bad.status_code == 401
    assert bye.status_code == 200

    agent = await store.get_agent("robot-h")
    assert agent is not None
    assert agent.reported_mcp_tools_list == ["robot.drive"]
    assert agent.health_fault == "low battery"
    assert agent.status == "offline"
    assert mcp_bridge.get_page_agent("robot-h") is None
