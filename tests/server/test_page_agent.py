"""Tests for the page-agent registration + heartbeat endpoints."""

from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_hub.config import ServerConfig, Settings
from agent_hub.registry.models import AgentKind
from agent_hub.registry.store import RegistryStore
from agent_hub.server import mcp_bridge
from agent_hub.server.page_agent import make_router as make_page_agent_router


async def _client(store: RegistryStore) -> AsyncClient:
    app = FastAPI()
    # No Access here, so page agents are only allowed with the opt-in.
    settings = Settings(server=ServerConfig(page_agents_allow_anonymous=True))
    app.include_router(make_page_agent_router(store, settings, {}))
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


async def test_register_cannot_take_over_a_board_by_its_id(store: RegistryStore) -> None:
    # Registering re-issues the row's token; with a board's MAC that locked
    # the board out of its voice socket and relabelled it as a page.
    await store.get_or_create_agent(
        device_id="aa:bb:cc:dd:ee:ff", kind=AgentKind.XIAOZHI, firmware_version="1.9.0"
    )
    board_token = await store.issue_websocket_token("aa:bb:cc:dd:ee:ff")
    async with await _client(store) as client:
        resp = await client.post(
            "/page-agent/register", json={"device_id": "aa:bb:cc:dd:ee:ff", "tools": []}
        )
    data = resp.json()
    assert resp.status_code == 200
    assert data["device_id"].startswith("page-")
    assert await store.validate_websocket_token("aa:bb:cc:dd:ee:ff", board_token)
    board = await store.get_agent("aa:bb:cc:dd:ee:ff")
    assert board is not None
    assert board.kind == AgentKind.XIAOZHI.value
    assert board.firmware_version == "1.9.0"
    mcp_bridge.unregister_page_agent(data["device_id"])


async def test_register_again_keeps_the_page_id(store: RegistryStore) -> None:
    # A tab reload re-registers with its stored id and must keep it.
    async with await _client(store) as client:
        first = await client.post("/page-agent/register", json={"device_id": "page-r", "tools": []})
        again = await client.post("/page-agent/register", json={"device_id": "page-r", "tools": []})
    assert first.json()["device_id"] == "page-r"
    assert again.json()["device_id"] == "page-r"
    mcp_bridge.unregister_page_agent("page-r")


async def test_register_is_refused_without_a_user_unless_allowed(store: RegistryStore) -> None:
    app = FastAPI()
    app.include_router(make_page_agent_router(store, Settings(), {}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/page-agent/register", json={"name": "kitchen", "tools": []})
    assert resp.status_code == 403
    assert "page_agents_allow_anonymous" in resp.json()["message"]
    assert await store.list_agents_with_personas() == []


async def test_anonymous_named_agent_is_owned_by_local_and_stable(store: RegistryStore) -> None:
    async with await _client(store) as client:
        first = await client.post("/page-agent/register", json={"name": "Kitchen", "tools": []})
        mcp_bridge.unregister_page_agent(first.json()["device_id"])
        again = await client.post(
            "/page-agent/register",
            # A different tab, casing, and spacing, and a spoofed id: still the
            # same agent.
            json={"name": "  kitchen ", "device_id": "page-other", "tools": []},
        )
    device_id = first.json()["device_id"]
    assert first.status_code == 200
    assert first.json()["name"] == "Kitchen"
    assert again.json()["device_id"] == device_id
    agent = await store.get_agent(device_id)
    assert agent is not None
    assert agent.owner == "local"
    assert agent.owner_subject is None
    assert await store.get_agent("page-other") is None
    mcp_bridge.unregister_page_agent(device_id)


async def test_named_agent_open_in_another_tab_needs_takeover(store: RegistryStore) -> None:
    async with await _client(store) as client:
        first = await client.post("/page-agent/register", json={"name": "desk", "tools": []})
        device_id = first.json()["device_id"]
        handle = mcp_bridge.get_page_agent(device_id)
        assert handle is not None
        handle.connected = True
        blocked = await client.post("/page-agent/register", json={"name": "desk", "tools": []})
        taken = await client.post(
            "/page-agent/register", json={"name": "desk", "takeover": True, "tools": []}
        )
    assert blocked.status_code == 409
    assert "already open" in blocked.json()["message"]
    assert await store.validate_websocket_token(device_id, first.json()["token"]) is False
    assert taken.status_code == 200
    assert taken.json()["device_id"] == device_id
    assert await store.validate_websocket_token(device_id, taken.json()["token"])
    mcp_bridge.unregister_page_agent(device_id)


async def test_register_rejects_an_unusable_name(store: RegistryStore) -> None:
    async with await _client(store) as client:
        long_name = await client.post("/page-agent/register", json={"name": "x" * 65, "tools": []})
        control = await client.post("/page-agent/register", json={"name": "a\x00b", "tools": []})
        not_text = await client.post("/page-agent/register", json={"name": 7, "tools": []})
    assert long_name.status_code == 400
    assert control.status_code == 400
    assert not_text.status_code == 400
