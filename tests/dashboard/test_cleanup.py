"""Stale-agent cleanup: thresholds per kind, the sweep, and the dashboard routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_hub.dashboard import cleanup
from agent_hub.dashboard.app import make_router
from agent_hub.registry.models import AgentKind
from agent_hub.registry.store import RegistryStore
from agent_hub.server import mcp_bridge


async def _backdate(store: RegistryStore, device_id: str, ago: timedelta) -> None:
    """Rewrite last_seen so an agent looks unseen for ``ago``."""
    from sqlalchemy import update

    from agent_hub.registry.models import Agent

    async with store._sessions() as session:
        await session.execute(
            update(Agent)
            .where(Agent.device_id == device_id)
            .values(last_seen=datetime.now(UTC) - ago)
        )
        await session.commit()


async def _client(store: RegistryStore, config: dict | None = None) -> AsyncClient:
    app = FastAPI()
    app.include_router(make_router(store, config or {}))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_stale_thresholds_differ_by_kind(store: RegistryStore) -> None:
    await store.get_or_create_agent("dev-old", kind=AgentKind.XIAOZHI)
    await store.get_or_create_agent("dev-recent", kind=AgentKind.XIAOZHI)
    await store.get_or_create_agent("page-old", kind=AgentKind.PAGE)
    await store.get_or_create_agent("page-recent", kind=AgentKind.PAGE)
    await _backdate(store, "dev-old", timedelta(days=15))
    await _backdate(store, "dev-recent", timedelta(days=3))
    await _backdate(store, "page-old", timedelta(hours=30))
    await _backdate(store, "page-recent", timedelta(hours=2))

    stale = await cleanup.find_stale(store, cleanup.StalePolicy())
    assert {a.device_id for a in stale} == {"dev-old", "page-old"}


async def test_prune_skips_live_agents_and_can_be_limited_to_pages(store: RegistryStore) -> None:
    await store.get_or_create_agent("dev-old", kind=AgentKind.XIAOZHI)
    await store.get_or_create_agent("page-old", kind=AgentKind.PAGE)
    await store.get_or_create_agent("page-live", kind=AgentKind.PAGE)
    for d in ("dev-old", "page-old", "page-live"):
        await _backdate(store, d, timedelta(days=30))
    # A page with its bridge stream open is live no matter how old the row is.
    mcp_bridge.register_page_agent("page-live", "tok", []).connected = True
    try:
        removed = await cleanup.prune(store, cleanup.StalePolicy(), kinds=cleanup.PAGE_ONLY)
    finally:
        mcp_bridge.unregister_page_agent("page-live")
    assert removed == ["page-old"]
    assert await store.get_agent("dev-old") is not None
    assert await store.get_agent("page-live") is not None


async def test_delete_agent_drops_history_but_keeps_spend(store: RegistryStore) -> None:
    await store.get_or_create_agent("page-x", kind=AgentKind.PAGE)
    await store.append_history("page-x", "user", "hi")
    await store.record_llm_spend(
        model="m",
        prompt_tokens=1,
        completion_tokens=1,
        cost_usd=0.5,
        cost_estimated=False,
        device_id="page-x",
    )
    assert await store.delete_agent("page-x") is True
    assert await store.get_agent("page-x") is None
    assert await store.load_history("page-x") == []
    assert (await store.llm_spend_by_device())["page-x"]["cost_usd"] == 0.5
    assert await store.delete_agent("page-x") is False


async def test_home_page_lists_stale_agents_and_prune_route_removes_them(
    store: RegistryStore,
) -> None:
    await store.get_or_create_agent("page-old", kind=AgentKind.PAGE, label="old tab")
    await _backdate(store, "page-old", timedelta(days=2))
    async with await _client(store) as c:
        home = await c.get("/dashboard/")
        assert "Cleanup" in home.text
        assert "old tab" in home.text
        assert "Remove 1 stale" in home.text
        pruned = await c.post("/dashboard/agents/prune")
    assert pruned.status_code == 200
    assert "Removed 1 agent(s)" in pruned.text
    assert await store.get_agent("page-old") is None


async def test_remove_route_deletes_one_agent(store: RegistryStore) -> None:
    await store.get_or_create_agent("dev-1", kind=AgentKind.XIAOZHI)
    async with await _client(store) as c:
        resp = await c.post("/dashboard/agents/dev-1/remove")
        missing = await c.post("/dashboard/agents/nope/remove")
    assert resp.status_code == 204
    assert resp.headers["HX-Redirect"] == "/dashboard/"
    assert missing.status_code == 404
    assert await store.get_agent("dev-1") is None


async def test_policy_reads_config() -> None:
    policy = cleanup.StalePolicy.from_config(
        {"registry": {"stale_device_days": 3, "stale_page_hours": 6}}
    )
    assert policy.device_after == timedelta(days=3)
    assert policy.page_after == timedelta(hours=6)
    assert "3 days" in policy.describe()
    assert "6 hours" in policy.describe()


async def test_pinned_agents_are_never_stale(store: RegistryStore) -> None:
    await store.get_or_create_agent("page-kept", kind=AgentKind.PAGE)
    await _backdate(store, "page-kept", timedelta(days=30))
    assert await store.set_agent_pinned("page-kept", True) is True
    assert await cleanup.find_stale(store, cleanup.StalePolicy()) == []
    assert await cleanup.prune(store, cleanup.StalePolicy()) == []
    await store.set_agent_pinned("page-kept", False)
    assert [a.device_id for a in await cleanup.find_stale(store, cleanup.StalePolicy())] == [
        "page-kept"
    ]


async def test_pin_route_toggles_and_shows_in_the_table(store: RegistryStore) -> None:
    await store.get_or_create_agent("dev-1", kind=AgentKind.XIAOZHI, label="lobby board")
    async with await _client(store) as c:
        on = await c.post("/dashboard/agents/dev-1/pin", data={"pinned": "1"})
        table = await c.get("/dashboard/agents")
        off = await c.post("/dashboard/agents/dev-1/pin", data={"pinned": "0"})
        missing = await c.post("/dashboard/agents/nope/pin", data={"pinned": "1"})
    assert on.status_code == 200 and "unpin" in on.text
    assert "kept" in table.text
    assert off.status_code == 200 and "Keep as long-term" in off.text
    assert missing.status_code == 404
    agent = await store.get_agent("dev-1")
    assert agent is not None and agent.pinned is False
