"""Conversations: settings, boundaries, storage, deletion, and the backfill."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from agent_hub.conversations import effective_settings
from agent_hub.registry.models import (
    AgentKind,
    Conversation,
    ConversationKind,
    ConversationTurn,
    Persona,
)
from agent_hub.registry.store import RegistryStore


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _agent(store: RegistryStore, device_id: str = "dev-1") -> Persona:
    await store.get_or_create_agent(device_id, kind=AgentKind.XIAOZHI)
    persona = await store.get_persona_for_device(device_id)
    assert persona is not None
    return persona


async def _age_conversation(store: RegistryStore, conversation_id: int, minutes: int) -> None:
    """Pretend the conversation's last turn was ``minutes`` ago."""
    async with store._sessions() as session:
        await session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(last_turn_at=_now() - timedelta(minutes=minutes))
        )
        await session.commit()


# ── settings ────────────────────────────────────────────────────────────────


async def test_settings_come_from_the_persona_unless_the_agent_overrides(
    store: RegistryStore,
) -> None:
    persona = await _agent(store)
    agent = await store.get_agent("dev-1")
    assert agent is not None

    plain = effective_settings(persona, agent)
    assert (plain.idle_minutes, plain.memory_window, plain.remember) == (30, 20, 3)
    assert set(plain.sources.values()) == {"persona"}

    agent.conversation_idle_minutes = 5
    agent.remember_conversations = 0
    overridden = effective_settings(persona, agent)
    assert overridden.idle_minutes == 5 and overridden.remember == 0
    assert overridden.sources["conversation_idle_minutes"] == "agent"
    assert overridden.sources["memory_window"] == "persona"


def test_settings_without_a_persona_use_the_defaults() -> None:
    settings = effective_settings(None)
    assert (settings.idle_minutes, settings.memory_window, settings.auto_title) == (30, 20, True)


# ── boundaries ──────────────────────────────────────────────────────────────


async def test_a_conversation_continues_within_the_idle_gap(store: RegistryStore) -> None:
    persona = await _agent(store)
    first = await store.open_conversation("dev-1", persona=persona, idle_minutes=30)
    assert first is not None
    await store.append_history("dev-1", "user", "hi", conversation_id=first.id)
    await _age_conversation(store, first.id, minutes=29)

    again = await store.open_conversation("dev-1", persona=persona, idle_minutes=30)
    assert again is not None and again.id == first.id


async def test_silence_past_the_idle_gap_starts_a_new_conversation(store: RegistryStore) -> None:
    persona = await _agent(store)
    first = await store.open_conversation("dev-1", persona=persona, idle_minutes=30)
    assert first is not None
    await store.append_history("dev-1", "user", "hi", conversation_id=first.id)
    await _age_conversation(store, first.id, minutes=31)

    # A device connecting doesn't start one…
    assert (
        await store.open_conversation("dev-1", persona=persona, idle_minutes=30, create=False)
        is None
    )
    # …a turn does, and the old one is ended at its last turn.
    second = await store.open_conversation("dev-1", persona=persona, idle_minutes=30)
    assert second is not None and second.id != first.id
    ended = await store.get_conversation(first.public_id)
    assert ended is not None and ended.ended_at == ended.last_turn_at


async def test_switching_persona_starts_a_new_conversation(store: RegistryStore) -> None:
    persona = await _agent(store)
    first = await store.open_conversation("dev-1", persona=persona, idle_minutes=30)
    assert first is not None
    await store.append_history("dev-1", "user", "hi", conversation_id=first.id)

    # Re-assigning the same persona (every page registration does) keeps it.
    await store.assign_persona("dev-1", persona.name)
    same = await store.open_conversation("dev-1", persona=persona, idle_minutes=30)
    assert same is not None and same.id == first.id

    await store.assign_persona("dev-1", "transcriber")
    transcriber = await store.get_persona_for_device("dev-1")
    after = await store.open_conversation("dev-1", persona=transcriber, idle_minutes=30)
    assert after is not None and after.id != first.id
    assert after.persona_name == "transcriber"


async def test_new_conversation_ends_the_open_one(store: RegistryStore) -> None:
    persona = await _agent(store)
    first = await store.open_conversation("dev-1", persona=persona, idle_minutes=30)
    assert first is not None
    assert await store.end_open_conversations("dev-1") == 1
    second = await store.open_conversation("dev-1", persona=persona, idle_minutes=30)
    assert second is not None and second.id != first.id


# ── storage ─────────────────────────────────────────────────────────────────


async def test_turns_update_the_conversation_and_load_per_conversation(
    store: RegistryStore,
) -> None:
    persona = await _agent(store)
    first = await store.open_conversation("dev-1", persona=persona, idle_minutes=30)
    assert first is not None
    await store.append_history("dev-1", "user", "old question", conversation_id=first.id)
    await store.append_history("dev-1", "assistant", "old answer", conversation_id=first.id)
    await store.end_open_conversations("dev-1")
    second = await store.open_conversation("dev-1", persona=persona, idle_minutes=30)
    assert second is not None
    await store.append_history("dev-1", "user", "new question", conversation_id=second.id)

    context = await store.load_history("dev-1", limit=40, conversation_id=second.id)
    assert [m["content"] for m in context] == ["new question"]
    first_now = await store.get_conversation(first.public_id)
    assert first_now is not None and first_now.turn_count == 1
    assert first_now.last_turn_at is not None
    assert [m["content"] for m in await store.conversation_messages(first.id)] == [
        "old question",
        "old answer",
    ]


async def test_transcript_conversations_follow_the_listen_session(store: RegistryStore) -> None:
    persona = await _agent(store)
    session_id = "20260917T101500Z-ab12"
    conversation = await store.transcript_conversation("dev-1", session_id, persona)
    same = await store.transcript_conversation("dev-1", session_id, persona)
    assert conversation.id == same.id
    assert conversation.public_id == session_id
    assert conversation.kind == ConversationKind.TRANSCRIPT.value

    await store.append_history(
        "dev-1", "transcript", "a line", session_id=session_id, conversation_id=conversation.id
    )
    await store.end_transcript_conversation(session_id)
    ended = await store.get_conversation(session_id)
    assert ended is not None and ended.ended_at is not None and ended.turn_count == 1


async def test_listing_skips_empty_conversations_and_pages_newest_first(
    store: RegistryStore,
) -> None:
    persona = await _agent(store)
    ids = []
    for i in range(3):
        conversation = await store.open_conversation("dev-1", persona=persona, idle_minutes=30)
        assert conversation is not None
        await store.append_history("dev-1", "user", f"turn {i}", conversation_id=conversation.id)
        await store.end_open_conversations("dev-1")
        ids.append(conversation.id)
    # Opened but never used: not listed.
    await store.open_conversation("dev-1", persona=persona, idle_minutes=30)

    page_one = await store.list_conversations("dev-1", limit=2)
    assert [c.id for c in page_one] == [ids[2], ids[1]]
    page_two = await store.list_conversations("dev-1", before_id=page_one[-1].id, limit=2)
    assert [c.id for c in page_two] == [ids[0]]


async def test_rename_and_delete(store: RegistryStore) -> None:
    persona = await _agent(store)
    conversation = await store.open_conversation("dev-1", persona=persona, idle_minutes=30)
    assert conversation is not None
    await store.append_history("dev-1", "user", "hi", conversation_id=conversation.id)

    assert await store.rename_conversation(conversation.public_id, "  Kitchen timer  ")
    renamed = await store.get_conversation(conversation.public_id)
    assert renamed is not None
    assert (renamed.title, renamed.title_source) == ("Kitchen timer", "manual")

    assert await store.delete_conversation(conversation.public_id)
    assert await store.get_conversation(conversation.public_id) is None
    assert await store.conversation_messages(conversation.id) == []


async def test_clearing_history_and_removing_agents_take_conversations_with_them(
    store: RegistryStore,
) -> None:
    for device_id in ("dev-clear", "dev-keep", "dev-drop"):
        persona = await _agent(store, device_id)
        conversation = await store.open_conversation(device_id, persona=persona, idle_minutes=30)
        assert conversation is not None
        await store.append_history(device_id, "user", "hi", conversation_id=conversation.id)

    await store.clear_history("dev-clear")
    await store.delete_agent("dev-keep", keep_history=True)
    await store.delete_agent("dev-drop")

    assert await store.list_conversations("dev-clear") == []
    assert len(await store.list_conversations("dev-keep")) == 1
    assert await store.list_conversations("dev-drop") == []


# ── backfill ────────────────────────────────────────────────────────────────


async def test_backfill_groups_old_history_into_conversations(store: RegistryStore) -> None:
    await _agent(store)
    base = _now() - timedelta(days=1)
    rows = [
        # A morning chat…
        ("user", "what's the weather in the morning today", None, 0),
        ("assistant", "Sunny.", None, 1),
        # …a transcriber session in between…
        ("transcript", "meeting notes line", "20260916T100000Z-aa11", 5),
        ("image", "[image:x.jpg] a whiteboard", "20260916T100000Z-aa11", 6),
        # …and an afternoon chat, hours later.
        ("user", "set a timer", None, 300),
        ("assistant", "Done.", None, 301),
    ]
    async with store._sessions() as session:
        for role, content, session_id, minutes in rows:
            session.add(
                ConversationTurn(
                    device_id="dev-1",
                    role=role,
                    content=content,
                    session_id=session_id,
                    created_at=base + timedelta(minutes=minutes),
                )
            )
        await session.commit()

    await store._backfill_conversations()
    await store._backfill_conversations()  # idempotent

    conversations = await store.list_conversations("dev-1")
    assert [(c.kind, c.title, c.turn_count) for c in conversations] == [
        ("chat", "set a timer", 1),
        ("transcript", "meeting notes line", 1),
        ("chat", "what's the weather in the morning today", 1),
    ]
    afternoon, transcript, morning = conversations
    assert transcript.public_id == "20260916T100000Z-aa11"
    assert {c.title_source for c in conversations} == {"fallback"}
    # Only the device's latest chat stays open, for the idle gap to decide.
    assert afternoon.ended_at is None
    assert morning.ended_at is not None and transcript.ended_at is not None
    assert [m["content"] for m in await store.conversation_messages(transcript.id)] == [
        "meeting notes line",
        "[image:x.jpg] a whiteboard",
    ]


async def test_backfill_titles_a_replies_only_conversation_from_its_first_reply(
    store: RegistryStore,
) -> None:
    await _agent(store)
    async with store._sessions() as session:
        session.add(
            ConversationTurn(
                device_id="dev-1",
                role="assistant",
                content="Sure, the oven timer is set for twelve minutes.\n[image:captured]",
                created_at=_now() - timedelta(days=2),
            )
        )
        await session.commit()
    await store._backfill_conversations()
    (conversation,) = await store.list_conversations("dev-1")
    assert conversation.title == "Sure, the oven timer is set for twelve…"
    assert conversation.turn_count == 0
