"""Agent-detail view + transcript download for a transcriber-assigned device."""

from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_hub.dashboard.app import make_router
from agent_hub.registry.store import RegistryStore


async def _client(store: RegistryStore) -> AsyncClient:
    app = FastAPI()
    app.include_router(make_router(store, {"server": {"timezone": "America/New_York"}}))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_transcriber_device_detail_relabels_and_hides_assistant_actions(
    store: RegistryStore,
) -> None:
    await store.get_or_create_agent("AA:BB:CC:DD:EE:01")
    await store.assign_persona("AA:BB:CC:DD:EE:01", "transcriber")

    async with await _client(store) as c:
        resp = await c.get("/dashboard/agents/AA:BB:CC:DD:EE:01")

    assert resp.status_code == 200
    assert "<h3>Transcript" in resp.text
    assert "transcript.txt" in resp.text
    assert "Send message to device" not in resp.text
    assert "Inject utterance" not in resp.text


async def test_normal_device_detail_is_unchanged(store: RegistryStore) -> None:
    await store.get_or_create_agent("AA:BB:CC:DD:EE:02")  # keeps hub-default

    async with await _client(store) as c:
        resp = await c.get("/dashboard/agents/AA:BB:CC:DD:EE:02")

    assert resp.status_code == 200
    assert "Conversation history" in resp.text
    assert "Send message to device" in resp.text
    assert "transcript.txt" not in resp.text


async def test_transcript_download_is_plain_text_with_the_logged_lines(
    store: RegistryStore,
) -> None:
    await store.get_or_create_agent("AA:BB:CC:DD:EE:03")
    await store.assign_persona("AA:BB:CC:DD:EE:03", "transcriber")
    await store.append_history("AA:BB:CC:DD:EE:03", "transcript", "first thing said")
    await store.append_history(
        "AA:BB:CC:DD:EE:03", "image", "[image:data/images/x.jpg] a robot on a bench"
    )
    await store.append_history("AA:BB:CC:DD:EE:03", "transcript", "second thing said")

    async with await _client(store) as c:
        resp = await c.get("/dashboard/agents/AA:BB:CC:DD:EE:03/transcript.txt")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "attachment; filename=" in resp.headers["content-disposition"]
    body = resp.text
    assert "first thing said" in body
    assert "second thing said" in body
    # the [image:...] marker is replaced with a readable [photo] tag
    assert "[photo] a robot on a bench" in body
    assert "[image:" not in body


async def test_transcript_download_404_for_unknown_device(store: RegistryStore) -> None:
    async with await _client(store) as c:
        resp = await c.get("/dashboard/agents/NO:SUCH:DEVICE/transcript.txt")
    assert resp.status_code == 404
