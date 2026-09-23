"""Paid models per person: new users are free, an admin allows paid per user.

Personas are shared, so the allowance is checked when a turn runs, against
the agent's owner. A free user's agent on a paid persona runs a free
fallback instead of spending; an agent nobody owns follows the hub default.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from agent_hub.dashboard import app as dashboard_app
from agent_hub.dashboard.access_identity import OperatorIdentity
from agent_hub.dashboard.authorization import DashboardAuthorization
from agent_hub.model_access import PaidModelNotAllowed, choose_model, operator_may_use_paid
from agent_hub.registry.models import AgentKind, OperatorRole
from agent_hub.registry.store import RegistryStore
from agent_hub.server import agent_turn, session_state

_PAID = "openai/gpt-4o-mini"
_FREE = "google/gemma-3-27b-it:free"


def _config(*, free_only: bool = True, fallbacks: str = _FREE) -> dict[str, Any]:
    return {
        "llm": {
            "free_only": free_only,
            "openai": {
                "api_key": "k",
                "base_url": "https://openrouter.ai/api/v1",
                "model": "google/gemma-4-31b-it",
                "fallback_models": fallbacks,
            },
        }
    }


async def _operator(
    store: RegistryStore, name: str, role: OperatorRole, *, paid: bool = False
) -> OperatorIdentity:
    identity = OperatorIdentity(email=f"{name}@example.com", subject=f"sub-{name}")
    await store.get_or_create_dashboard_operator(
        identity.subject, identity.email, set(), role.value
    )
    if paid:
        assert await store.update_dashboard_operator(
            identity.subject, role, enabled=True, paid_models=True
        )
    return identity


async def _agent_on_paid_persona(store: RegistryStore, owner: OperatorIdentity | None) -> str:
    device_id = f"robot-{owner.subject if owner else 'nobody'}"
    await store.get_or_create_agent(
        device_id,
        kind=AgentKind.MCP,
        owner=owner.email if owner else None,
        owner_subject=owner.subject if owner else None,
    )
    assert await store.update_persona_model("hub-default", _PAID)
    return device_id


async def _choice(store: RegistryStore, device_id: str, config: dict[str, Any]) -> str:
    persona = await store.get_persona_for_device(device_id)
    assert persona is not None
    return (await choose_model(store, config, persona, device_id)).model


async def test_new_users_are_free_and_run_a_free_fallback(store: RegistryStore) -> None:
    ada = await _operator(store, "ada", OperatorRole.OPERATOR)
    device_id = await _agent_on_paid_persona(store, ada)

    assert await _choice(store, device_id, _config()) == _FREE
    notice = session_state.get_state(device_id).model_notice
    assert _FREE in notice and _PAID in notice


async def test_paid_allowed_users_and_admins_run_the_paid_model(store: RegistryStore) -> None:
    ada = await _operator(store, "ada", OperatorRole.OPERATOR, paid=True)
    boss = await _operator(store, "boss", OperatorRole.ADMIN)
    for owner in (ada, boss):
        device_id = await _agent_on_paid_persona(store, owner)
        assert await _choice(store, device_id, _config()) == _PAID
        assert session_state.get_state(device_id).model_notice == ""


async def test_an_agent_nobody_owns_follows_the_hub_default(store: RegistryStore) -> None:
    device_id = await _agent_on_paid_persona(store, None)
    assert await _choice(store, device_id, _config()) == _FREE
    assert await _choice(store, device_id, _config(free_only=False)) == _PAID


async def test_without_a_free_substitute_the_turn_is_refused(store: RegistryStore) -> None:
    device_id = await _agent_on_paid_persona(store, None)
    persona = await store.get_persona_for_device(device_id)
    assert persona is not None
    with pytest.raises(PaidModelNotAllowed, match="free models only"):
        await choose_model(store, _config(fallbacks=""), persona, device_id)


async def test_free_models_and_non_openrouter_endpoints_are_left_alone(
    store: RegistryStore,
) -> None:
    device_id = await _agent_on_paid_persona(store, None)
    assert await store.update_persona_model("hub-default", "other/model:free")
    assert await _choice(store, device_id, _config()) == "other/model:free"

    local = _config()
    local["llm"]["openai"]["base_url"] = "http://localhost:11434/v1"
    assert await store.update_persona_model("hub-default", "llama3")
    assert await _choice(store, device_id, local) == "llama3"


async def test_a_disabled_operator_is_not_allowed_paid(store: RegistryStore) -> None:
    ada = await _operator(store, "ada", OperatorRole.OPERATOR, paid=True)
    assert await store.update_dashboard_operator(ada.subject, OperatorRole.OPERATOR, enabled=False)
    assert operator_may_use_paid(await store.get_dashboard_operator(ada.subject)) is False


async def test_text_turns_run_on_the_chosen_model(
    store: RegistryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    ada = await _operator(store, "ada", OperatorRole.OPERATOR)
    device_id = await _agent_on_paid_persona(store, ada)
    used: list[str | None] = []

    class _LLM:
        async def complete_with_tools(self, *args: Any, **kwargs: Any) -> str:
            return "hello"

    def _provider(name: str, config: dict[str, Any], model_override: str | None = None) -> _LLM:
        used.append(model_override)
        return _LLM()

    monkeypatch.setattr(agent_turn, "get_provider", _provider)
    result = await agent_turn.run_turn(store, _config(), device_id, "hi")
    assert result.reply == "hello"
    assert used == [_FREE]


# ── Dashboard: the picker follows the viewer, admins set the allowance ──────


class _AccessAuth(DashboardAuthorization):
    """Mirrors real Access authentication for a chosen identity."""

    def __init__(self, store: RegistryStore, identity: OperatorIdentity) -> None:
        super().__init__(store, {})
        self._identity = identity

    async def authenticate(self, request: Request) -> None:
        operator = await self._store.get_or_create_dashboard_operator(
            self._identity.subject, self._identity.email, set()
        )
        request.state.operator_identity = self._identity
        request.state.operator_role = operator.role
        request.state.paid_models = operator_may_use_paid(operator)


@pytest.fixture(autouse=True)
def _canned_models(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(_api_key: str) -> list[dict[str, Any]]:
        return [
            {"id": _FREE, "free": True, "tools": True, "multimodal": False},
            {"id": _PAID, "free": False, "tools": True, "multimodal": False},
        ]

    monkeypatch.setattr(dashboard_app, "_fetch_openrouter_models", _fake)
    monkeypatch.setattr(dashboard_app, "_models_cache", None)


async def _client(store: RegistryStore, identity: OperatorIdentity) -> AsyncClient:
    app = FastAPI()
    app.include_router(dashboard_app.make_router(store, _config(), _AccessAuth(store, identity)))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_only_paid_allowed_users_may_save_a_paid_model(store: RegistryStore) -> None:
    free_user = await _operator(store, "ada", OperatorRole.OPERATOR)
    paid_user = await _operator(store, "grace", OperatorRole.OPERATOR, paid=True)

    async with await _client(store, free_user) as client:
        refused = await client.post("/dashboard/models/select", data={"model_id": _PAID})
        page = await client.get("/dashboard/models")
    assert refused.status_code == 403
    assert "Operators page" in refused.text
    assert "free mode" in page.text

    async with await _client(store, paid_user) as client:
        allowed = await client.post("/dashboard/models/select", data={"model_id": _PAID})
        page = await client.get("/dashboard/models")
    assert allowed.status_code == 200
    assert 'class="badge badge-free"' not in page.text


async def test_admins_grant_paid_models_on_the_operators_page(store: RegistryStore) -> None:
    boss = await _operator(store, "boss", OperatorRole.ADMIN)
    ada = await _operator(store, "ada", OperatorRole.OPERATOR)

    async with await _client(store, boss) as client:
        page = await client.get("/dashboard/operators")
        assert 'name="paid_models" value="1" checked disabled' in page.text  # the admin row
        saved = await client.post(
            f"/dashboard/operators/{ada.subject}",
            data={"role": "operator", "enabled": "1", "paid_models": "1"},
        )
    assert saved.status_code == 200
    operator = await store.get_dashboard_operator(ada.subject)
    assert operator is not None and operator.paid_models is True


async def test_manage_page_says_when_a_free_model_stands_in(store: RegistryStore) -> None:
    ada = await _operator(store, "ada", OperatorRole.OPERATOR)
    device_id = await _agent_on_paid_persona(store, ada)
    await _choice(store, device_id, _config())
    async with await _client(store, ada) as client:
        status = await client.get(f"/dashboard/agents/{device_id}/status")
    assert f"Running {_FREE} instead of {_PAID}" in status.text
