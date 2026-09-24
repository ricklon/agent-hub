"""Running a page agent from its card or Manage page, not the page-agent picker.

Launch runs the agent in the fleet's side panel; Own window opens it in a
separate tab. Opening a name registers it as whoever is signed in, so both
are offered only to the agent's owner: anyone else following the link would
get their own agent of that name instead of this one.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from agent_hub.dashboard.access_identity import OperatorIdentity
from agent_hub.dashboard.app import make_router
from agent_hub.dashboard.authorization import DashboardAuthorization
from agent_hub.registry.models import Agent, AgentKind, OperatorRole
from agent_hub.registry.page_identity import (
    LOCAL_OWNER,
    named_page_device_id,
    page_agent_launch_url,
)
from agent_hub.registry.store import RegistryStore
from agent_hub.server import mcp_bridge
from agent_hub.server._page_html import PAGE_HTML

_RICK = OperatorIdentity(email="rick@example.com", subject="sub-rick")
_ADA = OperatorIdentity(email="ada@example.com", subject="sub-ada")
_LAUNCH = 'hx-get="/dashboard/page-agent/run?name=Kitchen+helper"'
_OWN_WINDOW = 'href="/dashboard/page-agent?name=Kitchen+helper" target="_blank"'


class _FakeAuth(DashboardAuthorization):
    """Authorization that asserts a chosen identity instead of verifying a JWT."""

    def __init__(self, store: RegistryStore, identity: OperatorIdentity, role: str) -> None:
        super().__init__(store, {})
        self._identity = identity
        self._role = role

    async def authenticate(self, request: Request) -> None:
        request.state.operator_identity = self._identity
        request.state.operator_role = self._role


async def _client(
    store: RegistryStore,
    identity: OperatorIdentity = _RICK,
    role: str = OperatorRole.OPERATOR.value,
) -> AsyncClient:
    app = FastAPI()
    app.include_router(make_router(store, {}, _FakeAuth(store, identity, role)))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _kitchen(store: RegistryStore, owner: OperatorIdentity = _RICK) -> str:
    device_id = named_page_device_id(owner.subject, "Kitchen helper")
    await store.get_or_create_agent(
        device_id,
        kind=AgentKind.PAGE,
        label="Kitchen helper",
        owner=owner.email,
        owner_subject=owner.subject,
    )
    return device_id


def _page(device_id: str, label: str, owner: str | None, subject: str | None) -> Agent:
    return Agent(
        device_id=device_id,
        kind=AgentKind.PAGE.value,
        label=label,
        owner=owner,
        owner_subject=subject,
    )


def test_launch_url_is_for_the_owner_of_a_named_page_agent_only() -> None:
    mine = _page(named_page_device_id("sub-rick", "Kitchen"), "Kitchen", "rick", "sub-rick")
    assert page_agent_launch_url(mine, "sub-rick") == "/dashboard/page-agent?name=Kitchen"
    assert page_agent_launch_url(mine, "sub-ada") is None
    assert page_agent_launch_url(mine, "") is None

    local = _page(named_page_device_id(LOCAL_OWNER, "Desk"), "Desk", LOCAL_OWNER, None)
    assert page_agent_launch_url(local, "") == "/dashboard/page-agent?name=Desk"
    # Signed in, the name would register under the viewer, not as this agent.
    assert page_agent_launch_url(local, "sub-rick") is None

    per_tab = _page("page-3f2a9c", "Tab", "rick", "sub-rick")
    assert page_agent_launch_url(per_tab, "sub-rick") is None


async def test_manage_page_offers_launch_and_interact_for_a_stopped_page_agent(
    store: RegistryStore,
) -> None:
    device_id = await _kitchen(store)
    async with await _client(store) as client:
        page = await client.get(f"/dashboard/agents/{device_id}")

    assert _LAUNCH in page.text
    assert f'hx-get="/dashboard/agents/{device_id}/conversation"' in page.text
    assert 'id="conversation-host"' in page.text
    # Near the top, not below the tool console and history.
    assert page.text.index(_LAUNCH) < page.text.index("<h3>Connection</h3>")


async def test_a_running_page_agent_offers_interact_not_launch(store: RegistryStore) -> None:
    device_id = await _kitchen(store)
    bridge = mcp_bridge.register_page_agent(device_id, "tok", [])
    bridge.connected = True
    try:
        async with await _client(store) as client:
            manage = await client.get(f"/dashboard/agents/{device_id}")
            cards = await client.get("/dashboard/")
    finally:
        mcp_bridge.unregister_page_agent(device_id)

    for page in (manage, cards):
        assert _LAUNCH not in page.text
        assert "data-conversation-open>Interact</a>" in page.text


@pytest.mark.parametrize(
    ("identity", "role"),
    [(_ADA, OperatorRole.ADMIN.value), (_RICK, OperatorRole.VIEWER.value)],
)
async def test_launch_is_hidden_from_other_owners_and_viewers(
    store: RegistryStore, identity: OperatorIdentity, role: str
) -> None:
    device_id = await _kitchen(store)
    async with await _client(store, identity, role) as client:
        manage = await client.get(f"/dashboard/agents/{device_id}")
        cards = await client.get("/dashboard/")

    for page in (manage, cards):
        assert _LAUNCH not in page.text
        assert f'hx-get="/dashboard/agents/{device_id}/conversation"' in page.text


async def test_fleet_card_offers_launch_to_the_owner(store: RegistryStore) -> None:
    await _kitchen(store)
    async with await _client(store) as client:
        cards = await client.get("/dashboard/")
    assert _LAUNCH in cards.text
    assert _OWN_WINDOW in cards.text


async def test_launch_runs_the_agent_in_the_side_panel(store: RegistryStore) -> None:
    async with await _client(store) as client:
        panel = await client.get("/dashboard/page-agent/run?name=Kitchen+helper&persona=chef")

    assert panel.status_code == 200
    assert 'data-running-agent="Kitchen helper"' in panel.text
    assert (
        '<iframe src="/dashboard/page-agent?name=Kitchen+helper&amp;persona=chef&amp;embed=1"'
        in panel.text
    )
    assert 'allow="microphone; camera; autoplay"' in panel.text
    # Moving it to its own window keeps the name and persona, without embed.
    assert (
        'href="/dashboard/page-agent?name=Kitchen+helper&amp;persona=chef&amp;handoff=1"'
        in panel.text
    )


async def test_viewers_cannot_run_agents_in_the_panel(store: RegistryStore) -> None:
    async with await _client(store, role=OperatorRole.VIEWER.value) as client:
        panel = await client.get("/dashboard/page-agent/run?name=Kitchen+helper")
    assert panel.status_code == 403


async def test_new_agents_start_from_a_card_not_the_nav(store: RegistryStore) -> None:
    async with await _client(store) as client:
        operator = await client.get("/dashboard/")
    async with await _client(store, role=OperatorRole.VIEWER.value) as client:
        viewer = await client.get("/dashboard/")

    assert "+ New browser agent" in operator.text
    assert 'hx-get="/dashboard/page-agent/run"' in operator.text
    assert '<option value="hub-default">' in operator.text
    assert ">Launch agent</a>" not in operator.text
    assert "+ New browser agent" not in viewer.text


def test_the_embedded_page_leaves_the_tab_memory_alone() -> None:
    assert 'get("embed") === "1"' in PAGE_HTML
    assert 'const tabName = EMBED ? "" : storeGet(sessionStorage, TAB_NAME_KEY);' in PAGE_HTML
    assert "if (!EMBED) storeSet(sessionStorage, TAB_NAME_KEY, agentName);" in PAGE_HTML


def test_page_opens_the_agent_named_in_the_url() -> None:
    assert 'const launchName = (query.get("name") || "").trim();' in PAGE_HTML
    # Only a move out of the side panel takes the agent over.
    assert 'await openAgent(query.get("handoff") === "1");' in PAGE_HTML
