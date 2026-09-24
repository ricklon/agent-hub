"""A persona chooses its TTS model: stored, migrated, edited, and used to speak."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from agent_hub.dashboard import app as dashboard_app
from agent_hub.dashboard import persona_options
from agent_hub.providers import tts as tts_pkg
from agent_hub.providers.tts.openrouter import OpenRouterTTSProvider
from agent_hub.registry.store import RegistryStore


async def test_an_existing_database_gains_the_column(tmp_path: Any) -> None:
    old = RegistryStore(db_path=tmp_path / "registry.db")
    await old.initialize()
    async with old._engine.begin() as conn:
        await conn.execute(text("ALTER TABLE personas DROP COLUMN tts_model"))
    await old._engine.dispose()

    store = RegistryStore(db_path=tmp_path / "registry.db")
    await store.initialize()
    persona = await store.get_persona_by_name("hub-default")
    assert persona is not None and persona.tts_model is None
    assert await store.update_persona("hub-default", tts_model="google/gemini-3.8-flash-tts")
    persona = await store.get_persona_by_name("hub-default")
    assert persona is not None and persona.tts_model == "google/gemini-3.8-flash-tts"
    # Blank clears it back to the hub default; None leaves it alone.
    assert await store.update_persona("hub-default", system_prompt="x")
    assert (await store.get_persona_by_name("hub-default")).tts_model is not None  # type: ignore[union-attr]
    assert await store.update_persona("hub-default", tts_model="")
    assert (await store.get_persona_by_name("hub-default")).tts_model is None  # type: ignore[union-attr]
    await store._engine.dispose()


def test_each_model_gets_its_own_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tts_pkg, "_cache", {})
    config = {"tts": {"openrouter": {"api_key": "k"}}}
    default = tts_pkg.get_provider("openrouter", config)
    flash = tts_pkg.get_provider("openrouter", config, "google/gemini-3.8-flash-tts")
    assert isinstance(default, OpenRouterTTSProvider)
    assert isinstance(flash, OpenRouterTTSProvider)
    assert default._model == "google/gemini-3.8-flash-lite-tts"
    assert flash._model == "google/gemini-3.8-flash-tts"
    assert tts_pkg.get_provider("openrouter", config, "google/gemini-3.8-flash-tts") is flash
    assert tts_pkg.get_provider("openrouter", config) is default


def test_edge_ignores_a_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tts_pkg, "_cache", {})
    assert tts_pkg.get_provider("edge", {}, "anything") is tts_pkg.get_provider("edge", {})


async def test_the_persona_model_reaches_the_voice(
    store: RegistryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page's hub-voice path synthesizes with the persona's model."""
    from agent_hub.server.page_agent import make_router

    seen: list[tuple[str, str | None]] = []

    class _FakeTTS:
        async def synthesize_pcm(self, text: str, voice: str | None = None) -> tuple[bytes, int]:
            return b"\x00\x00", 16000

    def _get(name: str, config: dict[str, Any], model: str | None = None) -> _FakeTTS:
        seen.append((name, model))
        return _FakeTTS()

    monkeypatch.setattr(tts_pkg, "get_provider", _get)
    await store.update_persona(
        "hub-default", tts_provider="openrouter", tts_model="google/gemini-3.8-flash-tts"
    )
    from agent_hub.config import ServerConfig, Settings

    app = FastAPI()
    settings = Settings(server=ServerConfig(page_agents_allow_anonymous=True))
    app.include_router(make_router(store, settings, {}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        reg = await client.post("/page-agent/register", json={"device_id": "page-m", "tools": []})
        token = reg.json()["token"]
        resp = await client.post(
            "/page-agent/tts", json={"device_id": "page-m", "token": token, "text": "Hello."}
        )
    assert resp.status_code == 200
    assert seen == [("openrouter", "google/gemini-3.8-flash-tts")]


@pytest.fixture()
def _no_catalogue(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _empty(_api_key: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(dashboard_app, "_fetch_openrouter_models", _empty)
    monkeypatch.setattr(dashboard_app, "_models_cache", None)


async def _dashboard(store: RegistryStore) -> AsyncClient:
    app = FastAPI()
    app.include_router(dashboard_app.make_router(store, {}))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.usefixtures("_no_catalogue")
async def test_the_editor_saves_checks_and_clears_the_model(store: RegistryStore) -> None:
    async with await _dashboard(store) as c:
        page = await c.get("/dashboard/personas/hub-default")
        saved = await c.post(
            "/dashboard/personas/hub-default",
            data={
                "tts_provider": "openrouter",
                "tts_voice": "Kore",
                "tts_model": " google/gemini-3.8-flash-tts ",
            },
        )
        after_save = await store.get_persona_by_name("hub-default")
        wrong_kitten = await c.post(
            "/dashboard/personas/hub-default",
            data={
                "tts_provider": "kitten",
                "tts_voice": "Luna",
                "tts_model": "google/gemini-3.8-flash-tts",
            },
        )
        to_edge = await c.post(
            "/dashboard/personas/hub-default",
            data={
                "tts_provider": "edge",
                "tts_voice": "en-US-AriaNeural",
                "tts_model": "google/gemini-3.8-flash-tts",
            },
        )
        after_edge = await store.get_persona_by_name("hub-default")
        swap = await c.get("/dashboard/persona-voices", params={"tts_provider": "openrouter"})
        listing = await c.get("/dashboard/personas")

    assert 'name="tts_model"' in page.text and 'list="tts-models"' in page.text
    assert saved.status_code == 200
    assert after_save is not None and after_save.tts_model == "google/gemini-3.8-flash-tts"
    assert wrong_kitten.status_code == 400
    assert "is not a kitten model" in wrong_kitten.text
    # Switching to Edge, which has no models, drops the model instead of refusing.
    assert to_edge.status_code == 200
    assert after_edge is not None
    assert (after_edge.tts_provider, after_edge.tts_model) == ("edge", None)
    # Changing the system swaps the model suggestions too.
    assert '<datalist id="tts-models" hx-swap-oob="true">' in swap.text
    assert "google/gemini-3.8-flash-lite-tts" in swap.text
    assert "edge / en-US-AriaNeural" in listing.text


def test_model_rules() -> None:
    assert persona_options.tts_model_problem("openrouter", "") is None
    assert persona_options.tts_model_problem("openrouter", "someone/new-tts") is None
    assert persona_options.tts_model_problem("kitten", "KittenML/kitten-tts-mini-0.8") is None
    assert persona_options.tts_model_problem("kitten", "nope") is not None
    assert persona_options.tts_model_problem("edge", "x") is not None
