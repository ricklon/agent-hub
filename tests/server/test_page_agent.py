"""Tests for the page-agent registration + heartbeat endpoints."""

from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_hub.config import Settings
from agent_hub.registry.models import AgentKind
from agent_hub.registry.store import RegistryStore
from agent_hub.server import mcp_bridge
from agent_hub.server.page_agent import make_router as make_page_agent_router


async def _client(store: RegistryStore) -> AsyncClient:
    app = FastAPI()
    app.include_router(make_page_agent_router(store, Settings(), {}))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_register_creates_page_agent_and_returns_token(store: RegistryStore) -> None:
    async with await _client(store) as client:
        resp = await client.post(
            "/page-agent/register",
            json={
                "device_id": "page-abc",
                "label": "classroom page",
                "tools": [
                    {
                        "name": "page.audio_speaker.speak",
                        "description": "speak",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                        },
                    }
                ],
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["device_id"] == "page-abc"
    assert data["token"]
    assert data["mcp_event_url"].endswith("/mcp/v1/events")
    assert data["mcp_respond_url"].endswith("/mcp/v1/respond")

    agent = await store.get_agent("page-abc")
    assert agent is not None
    assert agent.kind == AgentKind.PAGE.value


async def test_register_stores_tools_in_bridge(store: RegistryStore) -> None:
    async with await _client(store) as client:
        await client.post(
            "/page-agent/register",
            json={
                "device_id": "page-tools",
                "tools": [
                    {"name": "page.camera.take_photo", "description": "see", "inputSchema": {}}
                ],
            },
        )
    handle = mcp_bridge.find_page_agent_for_tool("page.camera.take_photo")
    assert handle is not None
    assert handle.device_id == "page-tools"
    mcp_bridge.unregister_page_agent("page-tools")


async def test_register_generates_device_id_when_omitted(store: RegistryStore) -> None:
    async with await _client(store) as client:
        resp = await client.post("/page-agent/register", json={"tools": []})
    data = resp.json()
    assert data["device_id"].startswith("page-")
    mcp_bridge.unregister_page_agent(data["device_id"])


async def test_heartbeat_rejects_bad_token(store: RegistryStore) -> None:
    async with await _client(store) as client:
        resp = await client.post(
            "/page-agent/heartbeat",
            json={"device_id": "page-x", "token": "wrong", "activity": "idle"},
        )
    assert resp.status_code == 401


async def test_heartbeat_accepts_valid_token(store: RegistryStore) -> None:
    await store.get_or_create_agent(device_id="page-hb", kind=AgentKind.PAGE)
    token = await store.issue_websocket_token("page-hb")
    async with await _client(store) as client:
        resp = await client.post(
            "/page-agent/heartbeat",
            json={
                "device_id": "page-hb",
                "token": token,
                "activity": "idle",
                "mcp_tools": ["page.audio_speaker.speak"],
            },
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    agent = await store.get_agent("page-hb")
    assert agent is not None
    assert agent.reported_mcp_tools_list == ["page.audio_speaker.speak"]


async def test_register_with_persona_assigns_it(store: RegistryStore) -> None:
    await store.create_persona("toaster3000", system_prompt="beep boop")
    async with await _client(store) as client:
        resp = await client.post(
            "/page-agent/register",
            json={"device_id": "page-persona", "tools": [], "persona": "toaster3000"},
        )
    assert resp.status_code == 200
    persona = await store.get_persona_for_device("page-persona")
    assert persona is not None
    assert persona.name == "toaster3000"
    mcp_bridge.unregister_page_agent("page-persona")


async def test_register_with_unknown_persona_is_ignored(store: RegistryStore) -> None:
    async with await _client(store) as client:
        resp = await client.post(
            "/page-agent/register",
            json={"device_id": "page-nopersona", "tools": [], "persona": "does-not-exist"},
        )
    assert resp.status_code == 200
    persona = await store.get_persona_for_device("page-nopersona")
    assert persona is not None
    assert persona.name == "hub-default"
    mcp_bridge.unregister_page_agent("page-nopersona")


async def test_page_agent_page_injects_the_persona_query(store: RegistryStore) -> None:
    async with await _client(store) as client:
        resp = await client.get("/dashboard/page-agent?persona=toaster3000")
    assert resp.status_code == 200
    assert "%%PERSONA%%" not in resp.text
    assert '"toaster3000"' in resp.text


async def test_goodbye_marks_the_page_offline_and_drops_the_bridge(store: RegistryStore) -> None:
    async with await _client(store) as client:
        reg = await client.post("/page-agent/register", json={"device_id": "page-bye", "tools": []})
        token = reg.json()["token"]
        assert mcp_bridge.get_page_agent("page-bye") is not None

        resp = await client.post(
            "/page-agent/goodbye", json={"device_id": "page-bye", "token": token}
        )
    assert resp.status_code == 200
    assert mcp_bridge.get_page_agent("page-bye") is None
    agent = await store.get_agent("page-bye")
    assert agent is not None
    assert agent.status == "offline"
    # No heartbeat left behind, so health reads offline right away rather
    # than after the heartbeat timeout.
    assert agent.last_heartbeat is None


async def test_goodbye_rejects_a_bad_token(store: RegistryStore) -> None:
    async with await _client(store) as client:
        await client.post("/page-agent/register", json={"device_id": "page-keep", "tools": []})
        resp = await client.post(
            "/page-agent/goodbye", json={"device_id": "page-keep", "token": "nope"}
        )
    assert resp.status_code == 401
    assert mcp_bridge.get_page_agent("page-keep") is not None


async def test_page_html_works_outside_secure_contexts(store: RegistryStore) -> None:
    """Plain http on a LAN address has no crypto.randomUUID and no mediaDevices."""
    async with await _client(store) as client:
        resp = await client.get("/dashboard/page-agent")
    html = resp.text
    # Identity must not depend on randomUUID being present …
    assert "crypto.getRandomValues" in html
    assert "if (crypto.randomUUID)" in html
    # … and must be per tab so two personas can run side by side.
    assert "sessionStorage.getItem" in html
    assert "localStorage." not in html
    # Media is only gated, never assumed.
    assert "needs https or localhost" in html
    assert "/page-agent/goodbye" in html


async def test_tts_speaks_with_the_persona_voice(store: RegistryStore, monkeypatch) -> None:
    """The page's hub-voice path returns WAV audio synthesized by the persona's TTS."""
    from agent_hub.providers import tts as tts_pkg

    calls: list[tuple[str, str | None]] = []

    class _FakeTTS:
        async def synthesize_pcm(self, text: str, voice: str | None = None) -> tuple[bytes, int]:
            calls.append((text, voice))
            return b"\x00\x00" * 160, 16000

    monkeypatch.setattr(tts_pkg, "get_provider", lambda name, config: _FakeTTS())
    async with await _client(store) as client:
        reg = await client.post("/page-agent/register", json={"device_id": "page-tts", "tools": []})
        token = reg.json()["token"]
        ok = await client.post(
            "/page-agent/tts", json={"device_id": "page-tts", "token": token, "text": "hello"}
        )
        bad = await client.post(
            "/page-agent/tts", json={"device_id": "page-tts", "token": "nope", "text": "hello"}
        )
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("audio/wav")
    assert ok.content[:4] == b"RIFF"
    assert calls == [("hello", None)]  # hub-default has no voice override
    assert bad.status_code == 401


async def test_page_html_lets_the_user_pick_hub_or_builtin_voice(store: RegistryStore) -> None:
    async with await _client(store) as client:
        resp = await client.get("/dashboard/page-agent")
    assert 'id="voiceMode"' in resp.text
    assert "/page-agent/tts" in resp.text
    assert "SpeechSynthesisUtterance" in resp.text
