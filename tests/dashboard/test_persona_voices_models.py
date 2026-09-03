"""Persona editor: voices follow the chosen TTS system; only tool-capable models are offered."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_hub.dashboard import app as dashboard_app
from agent_hub.dashboard import persona_options
from agent_hub.registry.store import RegistryStore

_MODELS: list[dict[str, Any]] = [
    {
        "id": "vendor/tooly",
        "name": "Tooly",
        "context_k": 32,
        "price_in": "free",
        "multimodal": False,
        "free": True,
        "tools": True,
    },
    {
        "id": "vendor/chatty",
        "name": "Chatty (no tools)",
        "context_k": 32,
        "price_in": "free",
        "multimodal": False,
        "free": True,
        "tools": False,
    },
]


@pytest.fixture(autouse=True)
def _canned_models(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(_api_key: str) -> list[dict[str, Any]]:
        return list(_MODELS)

    monkeypatch.setattr(dashboard_app, "_fetch_openrouter_models", _fake)
    monkeypatch.setattr(dashboard_app, "_models_cache", None)


async def _client(store: RegistryStore) -> AsyncClient:
    app = FastAPI()
    app.include_router(dashboard_app.make_router(store, {}))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def test_voices_follow_the_provider() -> None:
    assert persona_options.voices_for("edge") == persona_options.EDGE_VOICES
    assert persona_options.voices_for("kitten") == persona_options.KITTEN_VOICES
    assert persona_options.voices_for("nope") == ()
    assert persona_options.voice_problem("kitten", "Luna") is None
    assert persona_options.voice_problem("kitten", "") is None
    assert persona_options.voice_problem("kitten", "en-US-AriaNeural")
    # Edge has hundreds of voices; any id typed in is accepted.
    assert persona_options.voice_problem("edge", "de-DE-KatjaNeural") is None


async def test_voice_datalist_endpoint_swaps_with_the_system(store: RegistryStore) -> None:
    async with await _client(store) as c:
        kitten = await c.get("/dashboard/persona-voices", params={"tts_provider": "kitten"})
        edge = await c.get(
            "/dashboard/persona-voices", params={"tts_provider": "edge", "current": "custom-id"}
        )
    assert 'id="tts-voices"' in kitten.text
    assert "Luna" in kitten.text and "AriaNeural" not in kitten.text
    assert "AriaNeural" in edge.text and "Luna" not in edge.text
    assert "custom-id" in edge.text


async def test_edit_page_offers_voices_for_the_saved_system_only(store: RegistryStore) -> None:
    async with await _client(store) as c:
        page = await c.get("/dashboard/personas/hub-default")  # tts_provider = edge
    assert 'hx-get="/dashboard/persona-voices"' in page.text
    assert "AriaNeural" in page.text
    assert 'value="Luna"' not in page.text


async def test_save_refuses_a_voice_from_another_system(store: RegistryStore) -> None:
    async with await _client(store) as c:
        resp = await c.post(
            "/dashboard/personas/hub-default",
            data={"tts_provider": "kitten", "tts_voice": "en-US-AriaNeural"},
        )
    assert resp.status_code == 400
    assert "not a kitten voice" in resp.text
    persona = await store.get_persona_by_name("hub-default")
    assert persona is not None
    assert persona.tts_provider == "edge"


async def test_edit_page_lists_only_tool_capable_models(store: RegistryStore) -> None:
    async with await _client(store) as c:
        page = await c.get("/dashboard/personas/hub-default")
    assert 'id="llm-models"' in page.text
    assert "vendor/tooly" in page.text
    assert "vendor/chatty" not in page.text


async def test_models_page_hides_and_refuses_models_without_tools(store: RegistryStore) -> None:
    async with await _client(store) as c:
        listing = await c.get("/dashboard/models/list")
        refused = await c.post("/dashboard/models/select", data={"model_id": "vendor/chatty"})
        saved = await c.post("/dashboard/personas/hub-default", data={"llm_model": "vendor/chatty"})
        unknown = await c.post("/dashboard/models/select", data={"model_id": "local/ollama"})
    assert "vendor/tooly" in listing.text
    assert "vendor/chatty" not in listing.text
    assert "1 models without tool calling are hidden" in listing.text
    assert refused.status_code == 403 and "cannot call tools" in refused.text
    assert saved.status_code == 403
    # Ids the catalogue does not know (local models) are allowed.
    assert unknown.status_code == 200


async def test_transcription_toggle_script_is_on_the_edit_page(store: RegistryStore) -> None:
    async with await _client(store) as c:
        page = await c.get("/dashboard/personas/hub-default")
    assert "data-assistant-only" in page.text
    assert 'input[name="transcription"]' in page.text
