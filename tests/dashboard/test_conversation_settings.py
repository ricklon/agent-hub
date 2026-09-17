"""Conversation settings on the persona editor and as per-agent overrides."""

from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_hub.dashboard.app import make_router
from agent_hub.registry.models import AgentKind
from agent_hub.registry.store import RegistryStore


async def _client(store: RegistryStore) -> AsyncClient:
    app = FastAPI()
    app.include_router(make_router(store, {}))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_persona_editor_saves_conversation_settings(store: RegistryStore) -> None:
    async with await _client(store) as c:
        page = await c.get("/dashboard/personas/hub-default")
        saved = await c.post(
            "/dashboard/personas/hub-default",
            data={
                "llm_provider": "openai",
                "tts_provider": "edge",
                "asr_provider": "moonshine",
                "memory_window": "12",
                "conversation_idle_minutes": "45",
                "auto_title": "1",
                "remember_conversations": "5",
                # summarize_conversations unchecked: not sent
            },
        )
        # With summaries off the remember field is disabled and not submitted:
        # the number is kept, not reset.
        again = await c.post(
            "/dashboard/personas/hub-default",
            data={"llm_provider": "openai", "tts_provider": "edge", "asr_provider": "moonshine"},
        )
    assert "Conversations &amp; memory" in page.text
    assert 'name="conversation_idle_minutes"' in page.text
    assert saved.status_code == 200 and again.status_code == 200
    persona = await store.get_persona_by_name("hub-default")
    assert persona is not None
    assert (persona.conversation_idle_minutes, persona.auto_title) == (30, False)
    assert (persona.summarize_conversations, persona.remember_conversations) == (False, 5)


async def test_agent_page_overrides_and_resets_settings(store: RegistryStore) -> None:
    await store.get_or_create_agent("dev-1", kind=AgentKind.XIAOZHI)
    async with await _client(store) as c:
        page = await c.get("/dashboard/agents/dev-1")
        override = await c.post(
            "/dashboard/agents/dev-1/conversation_settings",
            data={"conversation_idle_minutes": "5", "auto_title": "off", "memory_window": ""},
        )
        bad = await c.post(
            "/dashboard/agents/dev-1/conversation_settings",
            data={"remember_conversations": "lots"},
        )
        agent_after_override = await store.get_agent("dev-1")
        reset = await c.post("/dashboard/agents/dev-1/conversation_settings", data={})
        missing = await c.post("/dashboard/agents/nope/conversation_settings", data={})

    assert 'id="conversation-settings"' in page.text
    assert "from hub-default" in page.text
    assert override.status_code == 200 and "this agent" in override.text
    assert agent_after_override is not None
    assert agent_after_override.conversation_idle_minutes == 5
    assert agent_after_override.auto_title is False
    assert agent_after_override.memory_window is None
    assert bad.status_code == 400 and "not a whole number" in bad.text
    assert reset.status_code == 200 and "this agent" not in reset.text
    agent = await store.get_agent("dev-1")
    assert agent is not None
    assert (agent.conversation_idle_minutes, agent.auto_title) == (None, None)
    assert missing.status_code == 404
