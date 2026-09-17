"""The dashboard's conversations: list, one conversation, exports, and context."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import agent_hub.dashboard.app as dashboard_app
from agent_hub.registry.models import AgentKind, Conversation
from agent_hub.registry.store import RegistryStore

_TZ = {"server": {"timezone": "America/New_York"}}


async def _client(store: RegistryStore, config: dict[str, Any] | None = None) -> AsyncClient:
    app = FastAPI()
    app.include_router(dashboard_app.make_router(store, config or _TZ))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _conversation(
    store: RegistryStore,
    device_id: str = "dev-1",
    turns: list[tuple[str, str]] | None = None,
    *,
    end: bool = True,
) -> Conversation:
    await store.get_or_create_agent(device_id, kind=AgentKind.XIAOZHI, label="kitchen board")
    persona = await store.get_persona_for_device(device_id)
    conversation = await store.open_conversation(device_id, persona=persona, idle_minutes=30)
    assert conversation is not None
    for role, content in turns or [("user", "set a timer"), ("assistant", "Done.")]:
        await store.append_history(device_id, role, content, conversation_id=conversation.id)
    if end:
        await store.end_open_conversations(device_id)
    result = await store.get_conversation(conversation.public_id)
    assert result is not None
    return result


async def test_the_agent_page_lists_conversations_instead_of_60_messages(
    store: RegistryStore,
) -> None:
    conversation = await _conversation(store)
    await store.save_wrap_up(
        conversation.id,
        title="Pasta timer",
        title_source="auto",
        summary="A 12-minute timer was set.",
        done=True,
        max_attempts=2,
    )
    async with await _client(store) as c:
        page = await c.get("/dashboard/agents/dev-1")
        listing = await c.get("/dashboard/agents/dev-1/conversations")

    assert "<h3>Conversations" in page.text
    assert "What the model sees next" in page.text
    assert 'hx-get="/dashboard/agents/dev-1/conversations"' in page.text
    assert "Delete all conversations" in page.text
    assert "Pasta timer" in listing.text
    assert "A 12-minute timer was set." in listing.text
    assert "1 turns" in listing.text


async def test_the_list_pages_instead_of_capping(store: RegistryStore) -> None:
    ids = [(await _conversation(store)).public_id for _ in range(dashboard_app._PAGE_SIZE + 2)]
    async with await _client(store) as c:
        first = await c.get("/dashboard/agents/dev-1/conversations")
        before = first.text.split("?before=")[1].split('"')[0]
        older = await c.get("/dashboard/agents/dev-1/conversations", params={"before": before})

    assert first.text.count("/conversations/") == dashboard_app._PAGE_SIZE
    assert "?before=" in first.text  # an Older button, not a cap
    assert ids[0] in older.text  # the oldest is only on the second page
    assert ids[0] not in first.text
    assert "?before=" not in older.text  # nothing older still


async def test_a_conversation_page_shows_every_message_with_days_and_times(
    store: RegistryStore,
) -> None:
    conversation = await _conversation(
        store, turns=[("user", "what did you see?"), ("assistant", "A mug.\n[image:x.jpg]")]
    )
    async with await _client(store) as c:
        page = await c.get(f"/dashboard/agents/dev-1/conversations/{conversation.public_id}")
        missing = await c.get("/dashboard/agents/dev-1/conversations/nope")
        other_agent = await c.get(f"/dashboard/agents/dev-2/conversations/{conversation.public_id}")

    assert page.status_code == 200
    assert "what did you see?" in page.text
    assert 'src="/dashboard/image?path=x.jpg"' in page.text  # photos inline
    assert "2 messages" in page.text
    assert "kitchen board" in page.text  # back link to the agent
    assert missing.status_code == 404
    # A conversation is only reachable under its own agent.
    assert other_agent.status_code == 404


async def test_rename_title_and_delete(
    store: RegistryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversation = await _conversation(store)
    called: list[bool] = []

    class _LLM:
        async def complete(self, messages: Any, system_prompt: str = "") -> str:
            called.append(True)
            return '{"title": "Named by the model", "summary": "A timer."}'

    monkeypatch.setattr("agent_hub.conversation_wrapup.get_provider", lambda *a, **k: _LLM())
    base = f"/dashboard/agents/dev-1/conversations/{conversation.public_id}"
    async with await _client(store) as c:
        renamed = await c.post(f"{base}/rename", data={"title": "Dinner"})
        # "Title this" asks the model even for a one-turn conversation, but a
        # manual title stands.
        titled = await c.post(f"{base}/title")
        saved = await store.get_conversation(conversation.public_id)
        deleted = await c.post(f"{base}/delete")

    assert renamed.status_code == 200 and "Renamed" in renamed.text
    assert titled.status_code == 200 and called == [True]
    assert saved is not None and (saved.title, saved.summary) == ("Dinner", "A timer.")
    assert deleted.status_code == 204
    assert deleted.headers["HX-Redirect"] == "/dashboard/agents/dev-1"
    assert await store.get_conversation(conversation.public_id) is None


async def test_exports_and_json(store: RegistryStore) -> None:
    conversation = await _conversation(store)
    await store.save_wrap_up(
        conversation.id,
        title="Pasta timer",
        title_source="auto",
        summary="A 12-minute timer.",
        done=True,
        max_attempts=2,
    )
    base = f"/dashboard/agents/dev-1/conversations/{conversation.public_id}"
    async with await _client(store) as c:
        text = await c.get(f"{base}.txt")
        markdown = await c.get(f"{base}.md")
        one = await c.get(f"{base}.json")
        many = await c.get("/dashboard/agents/dev-1/conversations.json")
        wrong = await c.get(f"{base}.pdf")

    assert "Pasta timer" in text.text and "set a timer" in text.text
    assert "dev-1-pasta-timer.txt" in text.headers["content-disposition"]
    assert markdown.text.startswith("# Pasta timer")
    assert "> A 12-minute timer." in markdown.text
    assert markdown.headers["content-type"].startswith("text/markdown")
    body = one.json()
    assert (body["title"], body["turns"], body["in_progress"]) == ("Pasta timer", 1, False)
    assert [m["content"] for m in body["messages"]] == ["set a timer", "Done."]
    assert [c["id"] for c in many.json()["conversations"]] == [conversation.public_id]
    assert wrong.status_code == 404


async def test_new_conversation_button_ends_the_open_one(store: RegistryStore) -> None:
    await _conversation(store, end=False)
    async with await _client(store) as c:
        listing = await c.post("/dashboard/agents/dev-1/conversations/new")
    assert "in progress" not in listing.text
    (only,) = await store.list_conversations("dev-1")
    assert only.ended_at is not None


async def test_what_the_model_sees_next(store: RegistryStore) -> None:
    first = await _conversation(store)
    await store.save_wrap_up(
        first.id,
        title="Diet",
        title_source="auto",
        summary="The user is vegetarian.",
        done=True,
        max_attempts=2,
    )
    await _conversation(
        store, turns=[("user", "suggest dinner"), ("image", "[photo] a mug")], end=False
    )
    async with await _client(store) as c:
        panel = await c.get("/dashboard/agents/dev-1/context")

    assert "Diet: The user is vegetarian." in panel.text
    assert "suggest dinner" in panel.text
    # Only what the model actually gets: photo rows are not sent.
    assert "a mug" not in panel.text
    assert "Recent turns (1)" in panel.text


async def test_the_old_transcript_link_redirects_to_the_conversation_export(
    store: RegistryStore,
) -> None:
    await store.get_or_create_agent("rec-1", kind=AgentKind.XIAOZHI)
    persona = await store.get_persona_for_device("rec-1")
    session_id = "20260917T100000Z-aa11"
    conversation = await store.transcript_conversation("rec-1", session_id, persona)
    await store.append_history(
        "rec-1", "transcript", "a line", session_id=session_id, conversation_id=conversation.id
    )
    async with await _client(store) as c:
        latest = await c.get("/dashboard/agents/rec-1/transcript.txt")
        named = await c.get(
            "/dashboard/agents/rec-1/transcript.txt", params={"session": session_id}
        )
        everything = await c.get(
            "/dashboard/agents/rec-1/transcript.txt", params={"session": "all"}
        )

    for resp in (latest, named):
        assert resp.status_code == 307
        assert resp.headers["location"] == (
            f"/dashboard/agents/rec-1/conversations/{session_id}.txt"
        )
    # "all" still exports the whole history.
    assert everything.status_code == 200 and "a line" in everything.text
