"""Tests for dashboard project documentation."""

from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_hub.dashboard.app import make_router as make_dashboard_router
from agent_hub.registry.store import RegistryStore


async def test_dashboard_docs_explains_project_and_protocol(store: RegistryStore) -> None:
    """Docs page should expose architecture, agent, and protocol context."""
    app = FastAPI()
    app.include_router(make_dashboard_router(store, {}))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/dashboard/docs")

    assert resp.status_code == 200
    assert "Project Documentation" in resp.text
    assert "Architecture" in resp.text
    assert "These Are Agents" in resp.text
    assert "xiaozhi-esp32 MCP Compatibility" in resp.text
    assert "/xiaozhi/ota/" in resp.text
    assert "No activation gate" in resp.text
    assert "Per-device personas" in resp.text
