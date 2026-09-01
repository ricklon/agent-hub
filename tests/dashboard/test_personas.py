"""Tests for the guided persona builder on the dashboard."""

from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import agent_hub.skills as skills
from agent_hub.dashboard.app import make_router
from agent_hub.registry.store import RegistryStore


async def _client(store: RegistryStore) -> AsyncClient:
    app = FastAPI()
    app.include_router(make_router(store, {}))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_personas_list_has_launch_link(store: RegistryStore) -> None:
    async with await _client(store) as c:
        resp = await c.get("/dashboard/personas")
    assert resp.status_code == 200
    assert "/dashboard/page-agent?persona=hub-default" in resp.text


async def test_new_persona_is_name_only_and_copies_hub_default(store: RegistryStore) -> None:
    await store.update_persona(
        "hub-default",
        tts_provider="kitten",
        tts_voice="Luna",
        asr_provider="moonshine",
        system_prompt="You are the base.",
    )
    async with await _client(store) as c:
        resp = await c.post("/dashboard/personas", data={"name": "toaster3000"})
    assert resp.status_code == 200

    made = await store.get_persona_by_name("toaster3000")
    assert made is not None
    assert (made.tts_provider, made.tts_voice, made.asr_provider) == (
        "kitten",
        "Luna",
        "moonshine",
    )
    assert made.system_prompt == "You are the base."


async def test_duplicate_name_is_rejected(store: RegistryStore) -> None:
    async with await _client(store) as c:
        resp = await c.post("/dashboard/personas", data={"name": "hub-default"})
    assert "already taken" in resp.text


async def test_edit_page_renders_guided_controls(store: RegistryStore) -> None:
    async with await _client(store) as c:
        resp = await c.get("/dashboard/personas/hub-default")
    assert resp.status_code == 200
    body = resp.text
    assert '<select name="tts_provider">' in body
    assert '<select name="asr_provider">' in body
    assert 'type="checkbox" name="server_skills"' in body
    assert 'name="preset"' in body
    assert 'id="system-prompt"' in body
    assert "/dashboard/page-agent?persona=hub-default" in body


async def test_preset_route_fills_the_prompt(store: RegistryStore) -> None:
    async with await _client(store) as c:
        resp = await c.get(
            "/dashboard/personas/hub-default/_preset",
            params={"preset": "Sardonic toaster"},
        )
    assert resp.status_code == 200
    assert 'id="system-prompt"' in resp.text
    assert "toaster" in resp.text.lower()


async def test_preset_route_keep_current_restores_saved_prompt(store: RegistryStore) -> None:
    await store.update_persona("hub-default", system_prompt="MY SAVED PROMPT")
    async with await _client(store) as c:
        resp = await c.get("/dashboard/personas/hub-default/_preset", params={"preset": ""})
    assert "MY SAVED PROMPT" in resp.text


async def test_save_with_a_skill_subset_pins_that_subset(store: RegistryStore) -> None:
    async with await _client(store) as c:
        resp = await c.post(
            "/dashboard/personas/hub-default",
            data={
                "system_prompt": "hi",
                "llm_provider": "openai",
                "tts_provider": "edge",
                "asr_provider": "funasr_onnx",
                "memory_window": "20",
                "server_skills": ["get_current_time"],
            },
        )
    assert resp.status_code == 200
    persona = await store.get_persona_by_name("hub-default")
    assert persona is not None
    assert persona.server_skills_list == ["get_current_time"]


async def test_save_with_all_skills_checked_stores_none(store: RegistryStore) -> None:
    all_names = [d["function"]["name"] for d in skills.get_definitions()]
    async with await _client(store) as c:
        resp = await c.post(
            "/dashboard/personas/hub-default",
            data={
                "system_prompt": "hi",
                "llm_provider": "openai",
                "tts_provider": "edge",
                "asr_provider": "funasr_onnx",
                "memory_window": "20",
                "server_skills": all_names,
            },
        )
    assert resp.status_code == 200
    persona = await store.get_persona_by_name("hub-default")
    assert persona is not None
    assert persona.server_skills_list is None


async def test_save_clears_the_device_tool_allowlist_back_to_defaults(store: RegistryStore) -> None:
    await store.update_persona("hub-default", mcp_tools_allowlist='["reboot"]')
    async with await _client(store) as c:
        await c.post(
            "/dashboard/personas/hub-default",
            data={
                "system_prompt": "hi",
                "tts_provider": "edge",
                "asr_provider": "funasr_onnx",
                "memory_window": "20",
                "server_skills": [d["function"]["name"] for d in skills.get_definitions()],
                "mcp_tools_allowlist": "",
            },
        )
    persona = await store.get_persona_by_name("hub-default")
    assert persona is not None
    assert persona.mcp_tools_allowlist_list is None
