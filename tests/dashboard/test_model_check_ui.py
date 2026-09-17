"""Model checks from the dashboard, the removed-model warning, and silent-turn reporting."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import agent_hub.dashboard.app as dashboard_app
from agent_hub.providers.llm.model_check import ModelCheck, Probe
from agent_hub.registry.models import AgentKind
from agent_hub.registry.store import RegistryStore
from agent_hub.server import session_state

_OPENROUTER = {
    "llm": {"openai": {"base_url": "https://openrouter.ai/api/v1", "model": "paid/default"}}
}


async def _client(store: RegistryStore, config: dict[str, Any] | None = None) -> AsyncClient:
    app = FastAPI()
    app.include_router(dashboard_app.make_router(store, config or _OPENROUTER))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _catalogue(*ids: str, free: bool = True) -> list[dict[str, Any]]:
    return [
        {
            "id": i,
            "name": i,
            "tools": True,
            "free": free,
            "multimodal": False,
            "context_k": 8,
            "price_in": "0",
        }
        for i in ids
    ]


@pytest.fixture
def checks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record which models were checked, without calling any provider."""
    seen: list[str] = []

    async def fake_check(llm: Any, model: str, **_: Any) -> ModelCheck:
        seen.append(model)
        return ModelCheck(
            model=model,
            verdict="unreliable",
            reason="1 of 3 turns failed: answered without calling the tool",
            probes=[
                Probe(
                    prompt="What time is it?",
                    expects_tool=True,
                    ok=False,
                    total_s=0.8,
                    first_text_s=0.4,
                    reply="<b>about three</b>",
                    tool_called=False,
                ),
            ],
            median_first_text_s=0.4,
        )

    monkeypatch.setattr(dashboard_app, "check_model", fake_check)
    monkeypatch.setattr(dashboard_app, "get_llm", lambda *a, **k: object())
    return seen


async def test_testing_a_model_reports_the_verdict_and_each_turn(
    store: RegistryStore, checks: list[str]
) -> None:
    async with await _client(store) as c:
        resp = await c.post("/dashboard/models/test", data={"model_id": "flaky:free"})
        default = await c.post("/dashboard/models/test", data={"model_id": ""})
        # The persona form's button submits the form's own fields.
        from_persona = await c.post(
            "/dashboard/models/test",
            data={"llm_model": "minimax/minimax-m3:free", "llm_provider": "openai"},
        )
    assert resp.status_code == 200
    assert "⚠ Unreliable" in resp.text and "flaky:free" in resp.text
    assert "answered without calling the tool" in resp.text
    # Model output is escaped, never rendered as markup.
    assert "&lt;b&gt;about three&lt;/b&gt;" in resp.text
    # A blank model id tests what a persona with no model of its own would run.
    assert checks == ["flaky:free", "paid/default", "minimax/minimax-m3:free"]
    assert default.status_code == 200 and from_persona.status_code == 200


async def test_free_mode_never_calls_a_paid_model_even_to_test_it(
    store: RegistryStore, checks: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def catalogue(api_key: str) -> list[dict[str, Any]]:
        return _catalogue("paid/model", free=False)

    monkeypatch.setattr(dashboard_app, "_fetch_openrouter_models", catalogue)
    config = {"llm": {**_OPENROUTER["llm"], "free_only": True}}
    async with await _client(store, config) as c:
        resp = await c.post("/dashboard/models/test", data={"model_id": "paid/model"})
        removed = await c.post("/dashboard/models/test", data={"model_id": "gone/model:free"})
    assert resp.status_code == 403
    assert "Free mode is on" in resp.text
    # A model the catalogue no longer lists is tested, so it can be reported as gone.
    assert removed.status_code == 200
    assert checks == ["gone/model:free"]


async def test_the_model_pages_offer_a_test(
    store: RegistryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def catalogue(api_key: str) -> list[dict[str, Any]]:
        return _catalogue("good:free")

    monkeypatch.setattr(dashboard_app, "_fetch_openrouter_models", catalogue)
    async with await _client(store) as c:
        picker = await c.get("/dashboard/models")
        rows = await c.get("/dashboard/models/list")
        persona = await c.get("/dashboard/personas/hub-default")
    assert "Test current model" in picker.text
    assert 'hx-post="/dashboard/models/test"' in rows.text and ">test</button>" in rows.text
    assert "Test model</button>" in persona.text


async def test_persona_page_warns_when_its_model_left_the_catalogue(
    store: RegistryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def catalogue(api_key: str) -> list[dict[str, Any]]:
        return _catalogue("good:free")

    monkeypatch.setattr(dashboard_app, "_fetch_openrouter_models", catalogue)
    async with await _client(store) as c:
        await store.update_persona_model("hub-default", "minimax/minimax-m3:free")
        gone = await c.get("/dashboard/personas/hub-default")
        await store.update_persona_model("hub-default", "good:free")
        fine = await c.get("/dashboard/personas/hub-default")
    assert "minimax/minimax-m3:free</code> is not in OpenRouter" in gone.text
    assert "is not in OpenRouter" not in fine.text


async def test_agent_status_shows_why_the_last_turn_got_no_reply(store: RegistryStore) -> None:
    await store.get_or_create_agent("dev-silent", kind=AgentKind.XIAOZHI)
    session_state.record_llm_error(
        "dev-silent",
        "minimax/minimax-m3:free",
        "not available from the provider (removed or renamed)",
    )
    async with await _client(store) as c:
        failing = await c.get("/dashboard/agents/dev-silent/status")
        session_state.record_turn("dev-silent", 100, 200, 300)
        recovered = await c.get("/dashboard/agents/dev-silent/status")
    assert "last turn got no reply" in failing.text
    assert "removed or renamed" in failing.text
    # A turn that works clears it.
    assert "last turn got no reply" not in recovered.text


async def test_dashboard_pages_show_html_refusals_instead_of_dropping_them(
    store: RegistryStore,
) -> None:
    async with await _client(store) as c:
        home = await c.get("/dashboard/")
    assert 'addEventListener("htmx:beforeSwap"' in home.text
    assert 'type.startsWith("text/html")' in home.text
