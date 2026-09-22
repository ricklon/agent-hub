"""Conversation destinations, voice parity, and dashboard trust boundaries."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from agent_hub.config import Settings
from agent_hub.dashboard import conversation
from agent_hub.dashboard.app import make_router
from agent_hub.dashboard.authorization import DashboardAuthorization
from agent_hub.registry.models import AgentKind
from agent_hub.registry.store import RegistryStore
from agent_hub.server import mcp_bridge, session_state
from agent_hub.server.agent_turn import TurnResult
from agent_hub.server.page_agent import make_router as page_router
from agent_hub.server.persona_voice import VOICE_TEST_TEXT


@pytest.fixture()
async def panel_client(store: RegistryStore) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.include_router(make_router(store, {}))
    app.include_router(page_router(store, Settings(), {}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    for device_id in ("panel-device", "panel-page", "panel-mcp"):
        session_state.unregister_session(device_id)
        session_state.set_pipeline_status(device_id, "idle")
        mcp_bridge.unregister_page_agent(device_id)


def bridge(device_id: str, *, speaker: bool = True) -> None:
    tools = (
        [
            {
                "name": "page.audio_speaker.speak",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}, "voice_mode": {"type": "string"}},
                    "required": ["text"],
                },
            }
        ]
        if speaker
        else []
    )
    mcp_bridge.register_page_agent(device_id, "test", tools).connected = True


async def test_device_messages_and_tests_use_onboard_speaker(
    store: RegistryStore, panel_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store.get_or_create_agent("panel-device", label="Bot <one>")
    inject = AsyncMock(return_value=("Hello <friend>", None))
    speak = AsyncMock()
    wrong_path = AsyncMock(side_effect=AssertionError("must use the device voice session"))
    monkeypatch.setattr(conversation, "run_turn", wrong_path)
    session_state.register_session("panel-device", speak, AsyncMock())
    session_state.register_injector("panel-device", inject)
    panel = await panel_client.get("/dashboard/agents/panel-device/conversation")
    assert "Bot &lt;one&gt;" in panel.text
    assert "onboard microphone" in panel.text
    assert "Send · device speaker" in panel.text
    reply = await panel_client.post(
        "/dashboard/agents/panel-device/conversation/send", data={"text": "hello"}
    )
    assert "Hello &lt;friend&gt;" in reply.text
    inject.assert_awaited_once_with("hello")
    await panel_client.post("/dashboard/agents/panel-device/conversation/voice-test")
    speak.assert_awaited_once_with(VOICE_TEST_TEXT)
    wrong_path.assert_not_called()


async def test_page_reply_explicitly_routes_persona_voice_to_its_tab(
    store: RegistryStore, panel_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store.get_or_create_agent("panel-page", kind=AgentKind.PAGE)
    bridge("panel-page")
    monkeypatch.setattr(conversation, "run_turn", AsyncMock(return_value=TurnResult("Hello")))
    call = AsyncMock(return_value="spoken via hub")
    monkeypatch.setattr(conversation, "call_one_tool", call)
    reply = await panel_client.post(
        "/dashboard/agents/panel-page/conversation/send", data={"text": "hi", "spoken": "1"}
    )
    assert reply.status_code == 200
    call.assert_awaited_once_with(
        "panel-page", "page.audio_speaker.speak", {"text": "Hello", "voice_mode": "hub"}
    )


async def test_mcp_without_speaker_is_text_only(
    store: RegistryStore, panel_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store.get_or_create_agent("panel-mcp", kind=AgentKind.MCP)
    bridge("panel-mcp", speaker=False)
    panel = await panel_client.get("/dashboard/agents/panel-mcp/conversation")
    assert "MCP tools determine" in panel.text
    assert "Test on" not in panel.text
    assert 'name="spoken"' not in panel.text
    tool_call = AsyncMock()
    monkeypatch.setattr(conversation, "call_one_tool", tool_call)
    result = await panel_client.post("/dashboard/agents/panel-mcp/conversation/voice-test")
    assert "does not advertise" in result.text
    tool_call.assert_not_called()


async def test_disconnected_and_busy_agents_do_not_start_turns(
    store: RegistryStore, panel_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store.get_or_create_agent("panel-mcp", kind=AgentKind.MCP)
    turn = AsyncMock()
    monkeypatch.setattr(conversation, "run_turn", turn)
    response = await panel_client.post(
        "/dashboard/agents/panel-mcp/conversation/send", data={"text": "go"}
    )
    assert "disconnected" in response.text
    bridge("panel-mcp")
    session_state.set_pipeline_status("panel-mcp", "thinking")
    response = await panel_client.post(
        "/dashboard/agents/panel-mcp/conversation/send", data={"text": "go"}
    )
    assert "busy" in response.text
    turn.assert_not_called()


async def test_live_history_escapes_model_text_and_voice_notice(
    store: RegistryStore, panel_client: AsyncClient
) -> None:
    await store.get_or_create_agent("panel-device")
    await store.append_history("panel-device", "assistant", "<script>alert(1)</script>")
    state = session_state.get_state("panel-device")
    state.voice_notice = "Fallback <voice>"
    state.first_audio_ms = 125
    response = await panel_client.get("/dashboard/agents/panel-device/conversation/live")
    assert "&lt;script&gt;" in response.text and "<script>" not in response.text
    assert "Fallback &lt;voice&gt;" in response.text
    assert "First audio sent: 125ms" in response.text


async def test_preview_and_page_use_identical_voice_and_sentence(
    store: RegistryStore, panel_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_hub.providers import tts

    await store.get_or_create_agent("panel-page", kind=AgentKind.PAGE)
    await store.update_persona("hub-default", tts_provider="edge", tts_voice="en-US-JennyNeural")
    token = await store.issue_websocket_token("panel-page")
    calls: list[tuple[str, str | None]] = []

    async def synthesize(text: str, voice: str | None = None) -> tuple[bytes, int]:
        calls.append((text, voice))
        return b"\x00\x00" * 160, 16000

    provider = SimpleNamespace(synthesize_pcm=synthesize)
    monkeypatch.setattr(conversation, "get_provider", lambda *args: provider)
    monkeypatch.setattr(tts, "get_provider", lambda *args: provider)
    preview = await panel_client.post("/dashboard/agents/panel-page/conversation/preview")
    page = await panel_client.post(
        "/page-agent/tts", json={"device_id": "panel-page", "token": token, "text": VOICE_TEST_TEXT}
    )
    assert preview.status_code == page.status_code == 200
    assert preview.content == page.content
    assert calls == [(VOICE_TEST_TEXT, "en-US-JennyNeural")] * 2


@pytest.mark.parametrize("action", ["send", "voice-test", "preview"])
async def test_panel_mutations_require_operator_and_same_origin(
    store: RegistryStore, panel_client: AsyncClient, action: str
) -> None:
    path = f"/dashboard/agents/panel-device/conversation/{action}"
    response = await panel_client.post(path, headers={"Origin": "https://untrusted.example"})
    assert response.status_code == 403
    auth = DashboardAuthorization(store, {})
    app = FastAPI()
    app.include_router(make_router(store, {}, auth))

    async def viewer(request: Request) -> None:
        request.state.operator_role = "viewer"

    app.dependency_overrides[auth.authenticate] = viewer
    await store.get_or_create_agent("panel-device")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(path)
        assert response.status_code == 403
        panel = await client.get("/dashboard/agents/panel-device/conversation")
        assert "Read-only access" in panel.text
        assert "data-voice-preview" not in panel.text


async def test_panel_requires_authentication(store: RegistryStore) -> None:
    app = FastAPI()
    app.include_router(make_router(store, {"server": {"dashboard_password": "secret"}}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/dashboard/agents/panel-device/conversation")
    assert response.status_code == 401
