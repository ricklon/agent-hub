"""Agent fleet cards and their browser-runtime capabilities."""

from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_hub.dashboard.app import make_router
from agent_hub.registry.models import AgentKind
from agent_hub.registry.store import RegistryStore
from agent_hub.server import mcp_bridge


async def _client(store: RegistryStore) -> AsyncClient:
    app = FastAPI()
    app.include_router(make_router(store, {}))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_page_agent_is_an_ordinary_card_with_live_browser_tools(
    store: RegistryStore,
) -> None:
    await store.get_or_create_agent("page-local", kind=AgentKind.PAGE, label="Pocket model")
    token = await store.issue_websocket_token("page-local")
    assert await store.record_authenticated_heartbeat(
        "page-local", token, None, "idle", ["stale.tool"]
    )
    bridge = mcp_bridge.register_page_agent(
        "page-local",
        "page-token",
        [
            {"name": "local_llm.generate", "description": "Run LiteRT LLM."},
            {"name": "webmcu.read_sensor", "description": "Read WebMCU sensor."},
            {"name": "page.camera.capture", "description": "Capture in browser."},
        ],
    )
    bridge.connected = True
    try:
        async with await _client(store) as client:
            page = await client.get("/dashboard/")
    finally:
        mcp_bridge.unregister_page_agent("page-local")

    assert 'class="agent-card health-' in page.text
    assert "Pocket model" in page.text
    assert "Browser runtime" in page.text
    assert "local_llm.generate" in page.text
    assert "webmcu.read_sensor" in page.text
    assert "stale.tool" not in page.text
    assert ">Interact</a>" in page.text
    assert ">Manage</a>" in page.text
    # Browser camera is a capability, not a firmware-only camera shortcut.
    assert ">Camera</a>" not in page.text


async def test_default_cards_poll_and_diagnostics_state_survives_navigation(
    store: RegistryStore,
) -> None:
    await store.get_or_create_agent(
        "robot-ada", kind=AgentKind.MCP, label="Ada bot", owner="ada@example.com"
    )
    async with await _client(store) as client:
        cards = await client.get("/dashboard/", params={"owner": "ada@example.com"})
        diagnostics = await client.get(
            "/dashboard/",
            params={"owner": "ada@example.com", "view": "diagnostics"},
        )

    assert (
        'id="agent-cards" hx-get="/dashboard/agents?owner=ada%40example.com" '
        'hx-trigger="every 5s"' in cards.text
    )
    assert 'href="/dashboard/?owner=ada%40example.com&amp;view=diagnostics"' in cards.text
    assert 'aria-current="page">Diagnostics</a>' in diagnostics.text
    assert 'hx-target="#agent-table"' in diagnostics.text
    assert (
        'hx-get="/dashboard/agents?owner=ada%40example.com&amp;view=diagnostics" '
        'hx-trigger="every 5s"' in diagnostics.text
    )


async def test_live_empty_tool_list_overrides_stale_heartbeat_tools(
    store: RegistryStore,
) -> None:
    await store.get_or_create_agent("robot-empty", kind=AgentKind.MCP)
    token = await store.issue_websocket_token("robot-empty")
    assert await store.record_authenticated_heartbeat(
        "robot-empty", token, None, "idle", ["stale.drive"]
    )
    bridge = mcp_bridge.register_page_agent("robot-empty", "robot-token", [])
    bridge.connected = True
    try:
        async with await _client(store) as client:
            page = await client.get("/dashboard/")
    finally:
        mcp_bridge.unregister_page_agent("robot-empty")

    assert "No tools reported" in page.text
    assert "stale.drive" not in page.text
    assert ">Interact</a>" in page.text
