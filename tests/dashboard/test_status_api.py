"""Tests for the dashboard device status JSON API."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_hub.dashboard.app import make_router
from agent_hub.registry.store import RegistryStore
from agent_hub.server import session_state


class _FakeMCPClient:
    def __init__(self) -> None:
        self.ready = True
        self.tools: dict[str, dict[str, Any]] = {
            "self_camera_take_photo": {
                "description": "Take a photo",
                "inputSchema": {"type": "object", "properties": {}},
            },
            "self_system_reboot": {
                "description": "Reboot the device",
                "inputSchema": {"type": "object", "properties": {}},
            },
        }


async def _noop_speak(text: str) -> None:
    _ = text


async def _noop_send_json(payload: dict[str, Any]) -> None:
    _ = payload


async def test_status_json_reports_capabilities_and_safe_effective_tools(
    store: RegistryStore,
) -> None:
    device_id = "AA:BB:CC:DD:EE:05"
    await store.get_or_create_agent(device_id, ip_address="192.0.2.5", firmware_version="3.5.0")
    session_state.register_session(device_id, _noop_speak, _noop_send_json)
    session_state.register_mcp_client(device_id, _FakeMCPClient())
    session_state.record_tool_result(
        device_id,
        name="get_weather",
        ok=False,
        text="Could not get weather.",
        error="backend unavailable",
    )
    session_state.record_turn(device_id, asr_ms=10, llm_ms=20, tts_ms=30)

    app = FastAPI()
    app.include_router(make_router(store, {}))

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/dashboard/agents/{device_id}/status.json")
    finally:
        session_state.unregister_session(device_id)

    assert resp.status_code == 200
    data = resp.json()
    assert data["device_id"] == device_id
    assert data["connected"] is True
    assert data["persona"]["name"] == "hub-default"
    assert data["persona"]["asr_provider"] == "funasr_onnx"
    assert data["mcp"]["connected"] is True
    assert data["mcp"]["ready"] is True
    assert data["mcp"]["tool_count"] == 2
    assert [tool["name"] for tool in data["mcp"]["tools"]] == [
        "self_camera_take_photo",
        "self_system_reboot",
    ]
    assert data["effective_tool_allowlist"] == ["self_camera_take_photo"]
    assert data["last_tool_results"] == [
        {
            "name": "get_weather",
            "ok": False,
            "text": "Could not get weather.",
            "error": "backend unavailable",
        }
    ]
    assert data["latency"]["last"]["total_ms"] == 60


async def test_status_json_returns_404_for_unknown_device(store: RegistryStore) -> None:
    app = FastAPI()
    app.include_router(make_router(store, {}))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/dashboard/agents/missing/status.json")

    assert resp.status_code == 404
