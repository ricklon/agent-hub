"""Claiming an agent as the signed-in operator, and the "mine" filter.

The typed owner label is supplied by an agent about itself, so it proves
nothing. A claim records the verified Cloudflare Access subject instead,
which is what makes "mine" trustworthy on a night when a dozen robots are
registering with whatever names their builders typed.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from agent_hub.dashboard.access_identity import OperatorIdentity
from agent_hub.dashboard.app import make_router
from agent_hub.dashboard.authorization import DashboardAuthorization
from agent_hub.registry.models import AgentKind, OperatorRole
from agent_hub.registry.store import RegistryStore

_RICK = OperatorIdentity(email="rick@example.com", subject="sub-rick")
_ADA = OperatorIdentity(email="ada@example.com", subject="sub-ada")


class _FakeAuth(DashboardAuthorization):
    """Authorization that asserts a chosen identity instead of verifying a JWT."""

    def __init__(
        self,
        store: RegistryStore,
        identity: OperatorIdentity | None,
        role: str = OperatorRole.OPERATOR.value,
    ) -> None:
        super().__init__(store, {})
        self._identity = identity
        self._role = role

    async def authenticate(self, request: Request) -> None:
        request.state.operator_identity = self._identity
        request.state.operator_role = self._role


async def _client(
    store: RegistryStore,
    identity: OperatorIdentity | None = _RICK,
    role: str = OperatorRole.OPERATOR.value,
) -> AsyncClient:
    app = FastAPI()
    app.include_router(make_router(store, {}, _FakeAuth(store, identity, role)))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _robot(store: RegistryStore, device_id: str = "robot-01") -> Any:
    return await store.get_or_create_agent(device_id, kind=AgentKind.MCP, label=device_id)


async def test_claim_records_the_verified_subject_not_a_typed_name(
    store: RegistryStore,
) -> None:
    await _robot(store)
    async with await _client(store) as c:
        resp = await c.post("/dashboard/agents/robot-01/claim")
    assert resp.status_code == 200
    assert "Claimed by you" in resp.text
    agent = await store.get_agent("robot-01")
    assert agent is not None
    assert agent.owner_subject == "sub-rick"
    assert agent.owner == "rick@example.com"


async def test_claim_is_refused_when_the_hub_has_no_verified_sign_in(
    store: RegistryStore,
) -> None:
    await _robot(store)
    async with await _client(store, identity=None) as c:
        resp = await c.post("/dashboard/agents/robot-01/claim")
    assert resp.status_code == 400
    assert "no verified sign-in" in resp.text
    agent = await store.get_agent("robot-01")
    assert agent is not None and agent.owner_subject is None


async def test_claim_on_a_missing_agent_is_a_404(store: RegistryStore) -> None:
    async with await _client(store) as c:
        resp = await c.post("/dashboard/agents/ghost/claim")
    assert resp.status_code == 404


async def test_you_can_release_your_own_claim(store: RegistryStore) -> None:
    await _robot(store)
    async with await _client(store) as c:
        await c.post("/dashboard/agents/robot-01/claim")
        resp = await c.post("/dashboard/agents/robot-01/release")
    assert resp.status_code == 200
    assert "Unclaimed" in resp.text
    agent = await store.get_agent("robot-01")
    assert agent is not None
    assert agent.owner_subject is None and agent.owner is None


async def test_someone_elses_claim_needs_an_admin_to_release(store: RegistryStore) -> None:
    await _robot(store)
    async with await _client(store, identity=_RICK) as rick:
        await rick.post("/dashboard/agents/robot-01/claim")

    async with await _client(store, identity=_ADA) as ada:
        refused = await ada.post("/dashboard/agents/robot-01/release")
    assert refused.status_code == 403
    assert "belongs to someone else" in refused.text
    agent = await store.get_agent("robot-01")
    assert agent is not None and agent.owner_subject == "sub-rick"

    async with await _client(store, identity=_ADA, role=OperatorRole.ADMIN.value) as admin:
        allowed = await admin.post("/dashboard/agents/robot-01/release")
    assert allowed.status_code == 200
    agent = await store.get_agent("robot-01")
    assert agent is not None and agent.owner_subject is None


async def test_an_unverified_label_is_shown_as_unverified(store: RegistryStore) -> None:
    """A robot registering with --owner ada proves nothing; say so."""
    await _robot(store)
    await store.set_agent_owner("robot-01", "ada")
    async with await _client(store) as c:
        page = await c.get("/dashboard/agents/robot-01")
    assert "unverified label" in page.text
    assert "Claim as rick@example.com" in page.text


async def test_the_agent_page_offers_a_claim_button_when_unclaimed(
    store: RegistryStore,
) -> None:
    await _robot(store)
    async with await _client(store) as c:
        page = await c.get("/dashboard/agents/robot-01")
    assert "Unclaimed" in page.text
    assert "/dashboard/agents/robot-01/claim" in page.text


async def test_mine_filters_on_the_claim_not_the_label(store: RegistryStore) -> None:
    await _robot(store, "robot-mine")
    await _robot(store, "robot-theirs")
    await _robot(store, "robot-impostor")
    async with await _client(store) as c:
        await c.post("/dashboard/agents/robot-mine/claim")
    # Ada's robot, claimed by Ada.
    async with await _client(store, identity=_ADA) as ada:
        await ada.post("/dashboard/agents/robot-theirs/claim")
    # A robot that merely *typed* Rick's name at registration.
    await store.set_agent_owner("robot-impostor", "rick@example.com")

    async with await _client(store) as c:
        mine = await c.get("/dashboard/agents", params={"mine": "1"})
        everyone = await c.get("/dashboard/agents")
    assert "robot-mine" in mine.text
    assert "robot-theirs" not in mine.text
    assert "robot-impostor" not in mine.text
    assert "robot-theirs" in everyone.text


async def test_the_mine_chip_only_appears_with_a_verified_identity(
    store: RegistryStore,
) -> None:
    await _robot(store)
    async with await _client(store) as signed_in:
        with_identity = await signed_in.get("/dashboard/")
    async with await _client(store, identity=None) as local:
        without = await local.get("/dashboard/")
        # The agent page explains why there is nothing to claim as.
        agent_page = await local.get("/dashboard/agents/robot-01")
    assert ">mine<" in with_identity.text
    assert ">mine<" not in without.text
    assert "no verified sign-in" in agent_page.text
    assert "Claim as" not in agent_page.text


@pytest.mark.parametrize("mine", ["1", ""])
async def test_the_table_keeps_its_filter_while_polling(store: RegistryStore, mine: str) -> None:
    await _robot(store)
    async with await _client(store) as c:
        resp = await c.get("/dashboard/agents", params={"mine": mine} if mine else {})
    expected = "/dashboard/agents?mine=1" if mine else "/dashboard/agents"
    assert f'hx-get="{expected}"' in resp.text
