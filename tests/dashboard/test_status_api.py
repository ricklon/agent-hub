"""Tests for the dashboard device status JSON API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
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


def test_health_and_activity_are_independent_dimensions() -> None:
    device_id = "AA:BB:CC:DD:EE:07"
    now = datetime.now(UTC)
    expected_activities = {
        "idle": "idle",
        "listening": "listening",
        "transcribing": "listening",
        "thinking": "thinking",
        "speaking": "speaking",
        "paused": "paused",
    }
    for phase, expected in expected_activities.items():
        session_state.set_pipeline_status(device_id, phase)
        assert session_state.get_device_activity(device_id) == expected
        assert session_state.get_device_health(device_id, now, None, 180, now=now) == "healthy"

    assert (
        session_state.get_device_health(
            device_id,
            now,
            "microphone unavailable",
            180,
            now=now,
        )
        == "degraded"
    )
    assert session_state.get_device_activity(device_id) == "paused"
    stale = now - timedelta(seconds=181)
    assert session_state.get_device_health(device_id, stale, None, 180, now=now) == "offline"


async def test_agents_list_displays_escaped_device_label(store: RegistryStore) -> None:
    device_id = "AA:BB:CC:DD:EE:06"
    await store.get_or_create_agent(device_id, label="UNIHIKER <K10>")
    token = await store.issue_websocket_token(device_id)
    assert await store.record_authenticated_heartbeat(device_id, token, None, "idle", [])
    app = FastAPI()
    app.include_router(make_router(store, {}))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/dashboard/agents")

    assert resp.status_code == 200
    assert "UNIHIKER &lt;K10&gt;" in resp.text
    assert "UNIHIKER <K10>" not in resp.text
    assert device_id in resp.text
    assert "Healthy" in resp.text
    assert "Idle" in resp.text
    assert "offline" not in resp.text.lower()


async def test_dashboard_home_guides_first_device_setup(store: RegistryStore) -> None:
    app = FastAPI()
    app.include_router(make_router(store, {}))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/dashboard/")

    assert response.status_code == 200
    assert "Connect your first agent" in response.text
    assert 'href="/dashboard/docs">Open setup guide</a>' in response.text
    assert 'href="/dashboard/personas">Prepare a persona</a>' in response.text
    assert "All agents" not in response.text


async def test_dashboard_home_prioritizes_agents_needing_attention(
    store: RegistryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    healthy_id = "AA:BB:CC:DD:EE:10"
    degraded_id = "AA:BB:CC:DD:EE:11"
    offline_id = "AA:BB:CC:DD:EE:12"
    await store.get_or_create_agent(healthy_id, label="Healthy device")
    await store.get_or_create_agent(degraded_id, label="Broken <microphone>")
    await store.get_or_create_agent(offline_id, label="Sleeping device")
    degraded_token = await store.issue_websocket_token(degraded_id)
    assert await store.record_authenticated_heartbeat(
        degraded_id,
        degraded_token,
        "microphone <unavailable>",
        "paused",
        [],
    )

    health: dict[str, session_state.DeviceHealth] = {
        healthy_id: "healthy",
        degraded_id: "degraded",
        offline_id: "offline",
    }

    def _device_health(device_id: str, *_args: Any, **_kwargs: Any) -> session_state.DeviceHealth:
        return health[device_id]

    monkeypatch.setattr(session_state, "get_device_health", _device_health)
    app = FastAPI()
    app.include_router(make_router(store, {}))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/dashboard/")

    assert response.status_code == 200
    assert "Fleet health" in response.text
    assert '<span class="overview-value">3</span>' in response.text
    assert '<span class="overview-label">Healthy</span>' in response.text
    assert '<span class="overview-label">Degraded</span>' in response.text
    assert '<span class="overview-label">Offline</span>' in response.text
    assert "Needs attention" in response.text
    assert '<span class="attention-count">2</span>' in response.text
    assert "Broken &lt;microphone&gt;" in response.text
    assert "microphone &lt;unavailable&gt;" in response.text
    assert "Sleeping device" in response.text
    assert response.text.count(">Inspect</a>") == 2
    assert "All agents" in response.text


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
    assert data["health"] == "healthy"
    assert data["activity"] == "idle"
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
