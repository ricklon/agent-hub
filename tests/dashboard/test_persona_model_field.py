"""Persona model field: a select that cannot come up empty, and says what you may pick.

Found live: OpenRouter withdrew a ``:free`` model, and the editor's text box,
pre-filled with the dead id, filtered its suggestions to nothing.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from agent_hub.dashboard import app as dashboard_app
from agent_hub.dashboard import model_field
from agent_hub.dashboard.access_identity import OperatorIdentity
from agent_hub.dashboard.authorization import DashboardAuthorization
from agent_hub.registry.models import OperatorRole
from agent_hub.registry.store import RegistryStore

_DEAD = "inclusionai/ling-3.0-flash-vl:free"
_PAID_VERSION = "inclusionai/ling-3.0-flash-vl"
_OPENROUTER = {"llm": {"free_only": True, "openai": {"base_url": "https://openrouter.ai/api/v1"}}}


def _model(model_id: str, *, free: bool, price: str = "free") -> dict[str, Any]:
    return {
        "id": model_id,
        "name": model_id.split("/")[-1],
        "context_k": 32,
        "price_in": price,
        "multimodal": False,
        "free": free,
        "tools": True,
    }


_MODELS = [
    _model("google/gemma-4-31b-it:free", free=True),
    _model("google/gemma-4-31b-it", free=False, price="$0.100"),
    _model(_PAID_VERSION, free=False, price="$0.050"),
]


@pytest.fixture(autouse=True)
def _canned_models(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(_api_key: str) -> list[dict[str, Any]]:
        return list(_MODELS)

    monkeypatch.setattr(dashboard_app, "_fetch_openrouter_models", _fake)
    monkeypatch.setattr(dashboard_app, "_models_cache", None)


class _FakeAuth(DashboardAuthorization):
    """Authorization that asserts an identity and paid allowance without a JWT."""

    def __init__(self, store: RegistryStore, role: str, paid: bool) -> None:
        super().__init__(store, {})
        self._role, self._paid = role, paid

    async def authenticate(self, request: Request) -> None:
        request.state.operator_identity = OperatorIdentity(email="r@example.com", subject="s")
        request.state.operator_role = self._role
        request.state.paid_models = self._paid


async def _editor(store: RegistryStore, *, paid: bool) -> str:
    await store.update_persona_model("hub-default", _DEAD)
    role = OperatorRole.ADMIN.value if paid else OperatorRole.OPERATOR.value
    app = FastAPI()
    app.include_router(dashboard_app.make_router(store, _OPENROUTER, _FakeAuth(store, role, paid)))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        page = await c.get("/dashboard/personas/hub-default")
    assert page.status_code == 200
    return page.text


def test_a_withdrawn_model_stays_selected_and_says_so() -> None:
    field = model_field.model_select(_DEAD, _MODELS, "google/gemma-4-31b-it")
    assert f'<option value="{_DEAD}" selected>' in field
    assert "no longer available, pick another" in field
    assert '<optgroup label="Free">' in field
    assert '<optgroup label="Paid (price per million input tokens)">' in field
    assert "$0.050/M in" in field
    assert "Hub default (google/gemma-4-31b-it)" in field


def test_the_paid_version_of_a_withdrawn_free_model_is_found() -> None:
    assert model_field.paid_version(_DEAD, _MODELS) == _PAID_VERSION
    assert model_field.paid_version("vendor/gone:free", _MODELS) is None
    assert model_field.paid_version(_PAID_VERSION, _MODELS) is None


async def test_an_admin_sees_paid_models_and_is_told_so(store: RegistryStore) -> None:
    page = await _editor(store, paid=True)
    assert '<select name="llm_model" id="llm-model"' in page
    assert "paid models allowed for you" in page
    assert f'<option value="{_PAID_VERSION}">' in page
    assert f"Its paid version <code>{_PAID_VERSION}</code> is still listed, under Paid." in page


async def test_someone_on_free_models_sees_only_free_ones(store: RegistryStore) -> None:
    page = await _editor(store, paid=False)
    assert "free mode: free models only" in page
    assert "Paid (price per million" not in page
    assert f'<option value="{_PAID_VERSION}">' not in page
    assert "(paid models are off for you)" in page
