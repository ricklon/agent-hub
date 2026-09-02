"""The dashboard renders stored UTC timestamps in the configured display zone."""

from __future__ import annotations

from datetime import datetime

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_hub.config import resolve_timezone
from agent_hub.dashboard._timefmt import fmt_ts
from agent_hub.dashboard.app import make_router
from agent_hub.registry.store import RegistryStore


def test_fmt_ts_converts_a_naive_utc_string_to_the_zone():
    ny = resolve_timezone("America/New_York")
    assert fmt_ts("2026-09-01T23:48:00", ny, "%H:%M") == "19:48"  # EDT = UTC-4


def test_fmt_ts_accepts_a_datetime():
    ny = resolve_timezone("America/New_York")
    assert fmt_ts(datetime(2026, 1, 1, 5, 0, 0), ny, "%H:%M") == "00:00"  # EST = UTC-5


def test_fmt_ts_handles_missing_and_unparsable_values():
    assert fmt_ts(None) == "—"
    assert fmt_ts("") == "—"
    assert fmt_ts("not-a-date") == "not-a-date"


async def test_history_partial_shows_local_time(store: RegistryStore) -> None:
    await store.get_or_create_agent("AA:BB:CC:DD:EE:FF")
    await store.append_history("AA:BB:CC:DD:EE:FF", "transcript", "a logged line")
    turns = await store.load_history("AA:BB:CC:DD:EE:FF")
    ny = resolve_timezone("America/New_York")
    expected = fmt_ts(turns[0]["created_at"], ny, "%Y-%m-%d %H:%M:%S")

    app = FastAPI()
    app.include_router(make_router(store, {"server": {"timezone": "America/New_York"}}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/dashboard/agents/AA:BB:CC:DD:EE:FF/history")

    assert resp.status_code == 200
    assert expected in resp.text
    # The stored value is UTC; NY is always 4-5h behind, so the raw hour differs.
    raw_utc = turns[0]["created_at"][:19].replace("T", " ")
    assert raw_utc not in resp.text
