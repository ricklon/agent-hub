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


async def _named_page(store: RegistryStore, name: str, identity: OperatorIdentity) -> str:
    from agent_hub.registry.page_identity import named_page_device_id

    device_id = named_page_device_id(identity.subject, name)
    await store.get_or_create_agent(
        device_id,
        kind=AgentKind.PAGE,
        label=name,
        owner=identity.email,
        owner_subject=identity.subject,
    )
    return device_id


async def test_the_fleet_table_is_grouped_by_owner_with_mine_first(store: RegistryStore) -> None:
    await _robot(store, "robot-mine")
    await _robot(store, "robot-ada")
    await _robot(store, "robot-typed")
    await _robot(store, "robot-nobody")
    async with await _client(store) as rick:
        await rick.post("/dashboard/agents/robot-mine/claim")
    async with await _client(store, _ADA) as ada:
        await ada.post("/dashboard/agents/robot-ada/claim")
    await store.set_agent_owner("robot-typed", "team-zeta")
    await store.get_or_create_agent("board-mine", kind=AgentKind.XIAOZHI, label="a board")
    await store.claim_agent("board-mine", _RICK.subject, _RICK.email)
    page = await _named_page(store, "kitchen", _RICK)

    async with await _client(store) as c:
        home = await c.get("/dashboard/")
    text = home.text
    order = [
        text.index(">Mine <"),
        text.index(">ada@example.com <"),
        text.index(">team-zeta (unverified label) <"),
        text.index(">Unowned <"),
    ]
    assert order == sorted(order)
    # Inside a section: boards, then robots, then pages.
    mine = text[order[0] : order[1]]
    assert mine.index("a board") < mine.index("robot-mine") < mine.index(page)


async def test_named_page_agents_cannot_change_hands(store: RegistryStore) -> None:
    page = await _named_page(store, "kitchen", _RICK)
    async with await _client(store, _ADA, OperatorRole.ADMIN.value) as ada:
        detail = await ada.get(f"/dashboard/agents/{page}")
        claim = await ada.post(f"/dashboard/agents/{page}/claim")
        release = await ada.post(f"/dashboard/agents/{page}/release")
        relabel = await ada.post(f"/dashboard/agents/{page}/owner", data={"owner": "ada"})

    # Changing the owner would orphan the agent: its id is derived from it.
    assert "Named page agent of rick@example.com" in detail.text
    assert "Claim as" not in detail.text
    assert claim.status_code == release.status_code == relabel.status_code == 409
    agent = await store.get_agent(page)
    assert agent is not None
    assert agent.owner_subject == _RICK.subject and agent.owner == _RICK.email


async def test_persona_page_lists_the_agents_that_use_it(store: RegistryStore) -> None:
    await _robot(store, "robot-mine")
    await store.claim_agent("robot-mine", _RICK.subject, _RICK.email)
    await _robot(store, "robot-nobody")
    async with await _client(store) as c:
        page = await c.get("/dashboard/personas/hub-default")
    assert "Used by 2 agents" in page.text
    assert page.text.index("<strong>Mine</strong>") < page.text.index("robot-mine")
    assert page.text.index("<strong>Unowned</strong>") < page.text.index("robot-nobody")


@pytest.mark.parametrize(
    ("query", "shown", "hidden"),
    [
        ({"mine": "1"}, "robot-mine", "robot-ada"),
        ({"owner": "ada@example.com"}, "robot-ada", "robot-mine"),
    ],
)
async def test_the_home_filter_is_never_replaced_by_a_refresh(
    store: RegistryStore, query: dict[str, str], shown: str, hidden: str
) -> None:
    await _robot(store, "robot-mine")
    await _robot(store, "robot-ada")
    await store.claim_agent("robot-mine", _RICK.subject, _RICK.email)
    await store.claim_agent("robot-ada", _ADA.subject, _ADA.email)
    async with await _client(store) as c:
        home = await c.get("/dashboard/", params=query)
        table_refresh = await c.get("/dashboard/agents", params=query)
        health_refresh = await c.get("/dashboard/overview", params=query)
    expected = "?mine=1" if "mine" in query else "?owner=ada%40example.com"

    # The chosen filter is shown as selected, and a chip swaps only the cards.
    assert f'aria-pressed="true" hx-get="/dashboard/agents{expected}"' in home.text
    assert 'hx-target="#agent-cards"' in home.text
    # The cards poll themselves with the same filter, and their refresh stays filtered.
    for resp in (home, table_refresh):
        cards = resp.text[resp.text.index('id="agent-cards"') :]
        assert f'hx-get="/dashboard/agents{expected}" hx-trigger="every 5s"' in cards
        assert shown in cards and hidden not in cards
    # The health poll carries no filter bar or cards, so it cannot clobber them,
    # and fleet health stays fleet-wide.
    assert 'id="fleet-health" hx-get="/dashboard/overview"' in health_refresh.text
    assert "owner-chip" not in health_refresh.text
    assert 'id="agent-cards"' not in health_refresh.text
    assert '<span class="overview-value">2</span>' in health_refresh.text


async def test_an_empty_fleet_switches_to_the_full_layout_when_an_agent_appears(
    store: RegistryStore,
) -> None:
    async with await _client(store) as c:
        empty = await c.get("/dashboard/")
        await _robot(store)
        fleet = await c.get("/dashboard/fleet")
    assert 'id="fleet-overview" hx-get="/dashboard/fleet"' in empty.text
    assert "All agents" in fleet.text and "robot-01" in fleet.text
    # Once there are agents the outer section stops polling.
    assert 'id="fleet-overview">' in fleet.text
