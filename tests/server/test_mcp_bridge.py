"""Tests for the page-agent MCP bridge (SSE-down / POST-up JSON-RPC)."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_hub.config import Settings
from agent_hub.registry.models import AgentKind
from agent_hub.registry.store import RegistryStore
from agent_hub.server import mcp_bridge
from agent_hub.server.mcp_bridge import make_router as make_mcp_bridge_router


def _tools() -> list[dict[str, object]]:
    return [
        {
            "name": "page.audio_speaker.speak",
            "description": "speak",
            "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
        }
    ]


async def _setup_page(store: RegistryStore, device_id: str = "page-bridge") -> str:
    await store.get_or_create_agent(device_id=device_id, kind=AgentKind.PAGE)
    token = await store.issue_websocket_token(device_id)
    mcp_bridge.register_page_agent(device_id, token, _tools())
    return token


async def test_call_page_tool_resolves_via_respond(store: RegistryStore) -> None:
    device_id = "page-bridge"
    token = await _setup_page(store, device_id)
    app = FastAPI()
    app.include_router(make_mcp_bridge_router(store, Settings()))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        call_task = asyncio.create_task(
            mcp_bridge.call_page_tool(device_id, "page.audio_speaker.speak", {"text": "hi"})
        )
        handle = mcp_bridge.get_page_agent(device_id)
        assert handle is not None
        request = await asyncio.wait_for(handle.outbound.get(), timeout=2.0)
        assert request["method"] == "tools/call"
        call_id = request["id"]

        resp = await client.post(
            "/mcp/v1/respond",
            json={
                "device_id": device_id,
                "token": token,
                "id": call_id,
                "result": {"content": [{"type": "text", "text": "spoken"}], "isError": False},
            },
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        result = await asyncio.wait_for(call_task, timeout=2.0)
    assert result == "spoken"
    mcp_bridge.unregister_page_agent(device_id)


async def test_call_page_tool_unknown_device_raises() -> None:
    with pytest.raises(KeyError):
        await mcp_bridge.call_page_tool("page-missing", "page.agent.status", {})


async def test_respond_rejects_unauthorized(store: RegistryStore) -> None:
    app = FastAPI()
    app.include_router(make_mcp_bridge_router(store, Settings()))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/mcp/v1/respond",
            json={"device_id": "page-x", "token": "nope", "id": 3, "result": {}},
        )
    assert resp.status_code == 401


async def test_respond_rejects_missing_id(store: RegistryStore) -> None:
    device_id = "page-noid"
    token = await _setup_page(store, device_id)
    app = FastAPI()
    app.include_router(make_mcp_bridge_router(store, Settings()))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/mcp/v1/respond",
            json={"device_id": device_id, "token": token, "result": {}},
        )
    assert resp.status_code == 400
    mcp_bridge.unregister_page_agent(device_id)


async def test_respond_rejects_unknown_id(store: RegistryStore) -> None:
    device_id = "page-stale"
    token = await _setup_page(store, device_id)
    app = FastAPI()
    app.include_router(make_mcp_bridge_router(store, Settings()))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/mcp/v1/respond",
            json={
                "device_id": device_id,
                "token": token,
                "id": 999,
                "result": {"content": [{"type": "text", "text": "x"}], "isError": False},
            },
        )
    assert resp.status_code == 404
    mcp_bridge.unregister_page_agent(device_id)


async def test_call_page_tool_times_out(store: RegistryStore) -> None:
    device_id = "page-slow"
    await _setup_page(store, device_id)
    with pytest.raises(TimeoutError):
        await mcp_bridge.call_page_tool(device_id, "page.agent.status", {}, timeout=0.1)
    # The pending future must be cleaned up after a timeout.
    handle = mcp_bridge.get_page_agent(device_id)
    assert handle is not None
    assert not handle.pending
    mcp_bridge.unregister_page_agent(device_id)


async def test_call_page_tool_propagates_page_error(store: RegistryStore) -> None:
    device_id = "page-err"
    token = await _setup_page(store, device_id)
    app = FastAPI()
    app.include_router(make_mcp_bridge_router(store, Settings()))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        call_task = asyncio.create_task(
            mcp_bridge.call_page_tool(device_id, "page.audio_speaker.speak", {"text": "x"})
        )
        handle = mcp_bridge.get_page_agent(device_id)
        assert handle is not None
        request = await asyncio.wait_for(handle.outbound.get(), timeout=2.0)
        await client.post(
            "/mcp/v1/respond",
            json={
                "device_id": device_id,
                "token": token,
                "id": request["id"],
                "error": {"code": -32603, "message": "denied"},
            },
        )
        with pytest.raises(RuntimeError, match="page error"):
            await asyncio.wait_for(call_task, timeout=2.0)
    mcp_bridge.unregister_page_agent(device_id)


async def test_event_generator_emits_tools_call_and_ping(store: RegistryStore) -> None:
    """The SSE generator yields a JSON-RPC tools/call when a call is enqueued,
    and a keep-alive comment when the queue is idle.

    The generator is the body of the StreamingResponse; verifying it directly
    avoids the httpx-ASGITransport limitation where a streaming response body
    is never made readable without a real socket.
    """
    device_id = "page-gen"
    await _setup_page(store, device_id)
    handle = mcp_bridge.get_page_agent(device_id)
    assert handle is not None

    gen = mcp_bridge.events_generator(handle)

    # Enqueue a tool call; the generator should yield it.
    call_task = asyncio.create_task(mcp_bridge.call_page_tool(device_id, "page.agent.status", {}))
    first = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
    assert first.startswith("data: ")
    payload = json.loads(first[len("data: ") :].strip())
    assert payload["method"] == "tools/call"
    call_id = payload["id"]

    # Resolve the call so the pending future completes.
    handle.pending[call_id].set_result(
        {"content": [{"type": "text", "text": "ok"}], "isError": False}
    )
    await asyncio.wait_for(call_task, timeout=2.0)

    # The next yield should be a keep-alive comment (queue empty → ping).
    second = await asyncio.wait_for(gen.__anext__(), timeout=20.0)
    assert second == ": ping\n\n"

    # Cancel the generator to trigger its finally block (connected=False).
    await gen.aclose()
    assert handle.connected is False
    mcp_bridge.unregister_page_agent(device_id)
