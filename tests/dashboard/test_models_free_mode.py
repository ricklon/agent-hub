"""Free mode: the model picker lists only free OpenRouter models and refuses paid ids."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_hub.dashboard import app as dashboard_app
from agent_hub.registry.store import RegistryStore

_MODELS: list[dict[str, Any]] = [
    {
        "id": "google/gemma-3-27b-it:free",
        "name": "Gemma 3 27B (free)",
        "context_k": 96,
        "price_in": "free",
        "multimodal": True,
        "free": True,
        "tools": True,
    },
    {
        "id": "openai/gpt-4o-mini",
        "name": "GPT-4o mini",
        "context_k": 128,
        "price_in": "$0.150",
        "multimodal": True,
        "free": False,
        "tools": True,
    },
]


@pytest.fixture(autouse=True)
def _canned_models(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(_api_key: str) -> list[dict[str, Any]]:
        return list(_MODELS)

    monkeypatch.setattr(dashboard_app, "_fetch_openrouter_models", _fake)
    monkeypatch.setattr(dashboard_app, "_models_cache", None)


async def _client(store: RegistryStore, config: dict[str, Any]) -> AsyncClient:
    app = FastAPI()
    app.include_router(dashboard_app.make_router(store, config))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_free_only_filter_hides_paid_models(store: RegistryStore) -> None:
    async with await _client(store, {}) as c:
        everything = await c.get("/dashboard/models/list")
        free = await c.get("/dashboard/models/list", params={"free": "1"})
    assert "openai/gpt-4o-mini" in everything.text
    assert "openai/gpt-4o-mini" not in free.text
    assert "google/gemma-3-27b-it:free" in free.text


async def test_free_mode_config_forces_the_filter_and_shows_a_badge(store: RegistryStore) -> None:
    async with await _client(store, {"llm": {"free_only": True}}) as c:
        page = await c.get("/dashboard/models")
        listing = await c.get("/dashboard/models/list")
    assert "free mode" in page.text
    assert "checked disabled" in page.text
    assert "openai/gpt-4o-mini" not in listing.text
    assert "google/gemma-3-27b-it:free" in listing.text


async def test_free_mode_refuses_selecting_a_paid_model(store: RegistryStore) -> None:
    async with await _client(store, {"llm": {"free_only": True}}) as c:
        paid = await c.post("/dashboard/models/select", data={"model_id": "openai/gpt-4o-mini"})
        free = await c.post(
            "/dashboard/models/select", data={"model_id": "google/gemma-3-27b-it:free"}
        )
    assert paid.status_code == 403
    assert free.status_code == 200
    persona = await store.get_persona_by_name("hub-default")
    assert persona is not None
    assert persona.llm_model == "google/gemma-3-27b-it:free"


async def test_free_mode_refuses_saving_a_paid_model_on_a_persona(store: RegistryStore) -> None:
    async with await _client(store, {"llm": {"free_only": True}}) as c:
        resp = await c.post(
            "/dashboard/personas/hub-default",
            data={"llm_model": "openai/gpt-4o-mini", "system_prompt": "hi"},
        )
    assert resp.status_code == 403
    persona = await store.get_persona_by_name("hub-default")
    assert persona is not None
    assert persona.llm_model != "openai/gpt-4o-mini"


async def test_without_free_mode_paid_models_are_selectable(store: RegistryStore) -> None:
    async with await _client(store, {}) as c:
        resp = await c.post("/dashboard/models/select", data={"model_id": "openai/gpt-4o-mini"})
    assert resp.status_code == 200


async def test_is_free_model_falls_back_to_suffix_when_catalogue_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _none(_api_key: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(dashboard_app, "_fetch_openrouter_models", _none)
    assert await dashboard_app.is_free_model("x/y:free", "") is True
    assert await dashboard_app.is_free_model("x/y", "") is False
