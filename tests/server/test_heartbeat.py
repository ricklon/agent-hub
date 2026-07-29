"""Tests for the authenticated device heartbeat contract."""

from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_hub.config import ServerConfig, Settings
from agent_hub.registry.store import RegistryStore
from agent_hub.server.heartbeat import make_router

_DEVICE = "AA:BB:CC:DD:EE:88"


def _client(store: RegistryStore, interval: int = 60) -> AsyncClient:
    app = FastAPI()
    app.include_router(
        make_router(store, Settings(server=ServerConfig(heartbeat_interval_seconds=interval)))
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_heartbeat_requires_device_bearer_token(store: RegistryStore) -> None:
    await store.get_or_create_agent(_DEVICE)
    async with _client(store) as client:
        response = await client.post(
            "/xiaozhi/heartbeat/",
            headers={"Device-Id": _DEVICE},
            json={"health": "healthy", "activity": "paused"},
        )

    assert response.status_code == 401
    assert response.json()["message"] == "invalid device token"


async def test_healthy_heartbeat_updates_liveness_and_returns_interval(
    store: RegistryStore,
) -> None:
    await store.get_or_create_agent(_DEVICE)
    token = await store.issue_websocket_token(_DEVICE)
    async with _client(store, interval=45) as client:
        response = await client.post(
            "/xiaozhi/heartbeat/",
            headers={"Device-Id": _DEVICE, "Authorization": f"Bearer {token}"},
            json={
                "health": "healthy",
                "activity": "paused",
                "mcp_tools": ["self.get_device_status", "self.camera.take_photo"],
            },
        )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["next_heartbeat_seconds"] == 45
    agent = await store.get_agent(_DEVICE)
    assert agent is not None
    assert agent.last_heartbeat is not None
    assert agent.health_fault is None
    assert agent.reported_activity == "paused"
    assert agent.reported_mcp_tools_list == [
        "self.get_device_status",
        "self.camera.take_photo",
    ]


async def test_degraded_heartbeat_records_fault_and_healthy_clears_it(
    store: RegistryStore,
) -> None:
    await store.get_or_create_agent(_DEVICE)
    token = await store.issue_websocket_token(_DEVICE)
    headers = {"Device-Id": _DEVICE, "Authorization": f"Bearer {token}"}
    async with _client(store) as client:
        degraded = await client.post(
            "/xiaozhi/heartbeat/",
            headers=headers,
            json={"health": "degraded", "fault": "microphone unavailable"},
        )
        agent = await store.get_agent(_DEVICE)
        healthy = await client.post(
            "/xiaozhi/heartbeat/",
            headers=headers,
            json={"health": "healthy"},
        )

    assert degraded.status_code == 200
    assert agent is not None
    assert agent.health_fault == "microphone unavailable"
    assert healthy.status_code == 200
    refreshed = await store.get_agent(_DEVICE)
    assert refreshed is not None
    assert refreshed.health_fault is None


async def test_heartbeat_rejects_unknown_health_value(store: RegistryStore) -> None:
    await store.get_or_create_agent(_DEVICE)
    token = await store.issue_websocket_token(_DEVICE)
    async with _client(store) as client:
        response = await client.post(
            "/xiaozhi/heartbeat/",
            headers={"Device-Id": _DEVICE, "Authorization": f"Bearer {token}"},
            json={"health": "confused"},
        )

    assert response.status_code == 400


async def test_heartbeat_rejects_unknown_activity(store: RegistryStore) -> None:
    await store.get_or_create_agent(_DEVICE)
    token = await store.issue_websocket_token(_DEVICE)
    async with _client(store) as client:
        response = await client.post(
            "/xiaozhi/heartbeat/",
            headers={"Device-Id": _DEVICE, "Authorization": f"Bearer {token}"},
            json={"health": "healthy", "activity": "confused"},
        )

    assert response.status_code == 400


async def test_heartbeat_records_activity_independently_of_health(
    store: RegistryStore,
) -> None:
    await store.get_or_create_agent(_DEVICE)
    token = await store.issue_websocket_token(_DEVICE)
    async with _client(store) as client:
        response = await client.post(
            "/xiaozhi/heartbeat/",
            headers={"Device-Id": _DEVICE, "Authorization": f"Bearer {token}"},
            json={"health": "healthy", "activity": "listening"},
        )

    assert response.status_code == 200
    agent = await store.get_agent(_DEVICE)
    assert agent is not None
    assert agent.health_fault is None
    assert agent.reported_activity == "listening"
