"""Wrap-up (titles and summaries), the idle sweep, and remembered conversations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import update

from agent_hub import conversation_wrapup
from agent_hub.conversations import effective_settings, memory_note, memory_note_for_turn
from agent_hub.registry.models import AgentKind, Conversation
from agent_hub.registry.store import RegistryStore
from agent_hub.server import agent_turn


class _ScriptedLLM:
    def __init__(self, replies: list[Any]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[list[dict[str, str]], str]] = []

    async def complete(self, messages: list[dict[str, str]], system_prompt: str = "") -> str:
        self.calls.append((messages, system_prompt))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return str(reply)


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> _ScriptedLLM:
    fake = _ScriptedLLM([])
    monkeypatch.setattr(conversation_wrapup, "get_provider", lambda *a, **k: fake)
    return fake


def _utc(minutes_ago: float = 0) -> datetime:
    return datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=minutes_ago)


async def _ended_conversation(
    store: RegistryStore,
    device_id: str = "dev-1",
    turns: list[tuple[str, str]] | None = None,
) -> Conversation:
    await store.get_or_create_agent(device_id, kind=AgentKind.XIAOZHI)
    persona = await store.get_persona_for_device(device_id)
    conversation = await store.open_conversation(device_id, persona=persona, idle_minutes=30)
    assert conversation is not None
    for role, content in turns or [
        ("user", "set a timer for pasta"),
        ("assistant", "Twelve minutes, done."),
        ("user", "how long for fettuccine?"),
        ("assistant", "About eleven minutes."),
    ]:
        await store.append_history(device_id, role, content, conversation_id=conversation.id)
    await store.end_open_conversations(device_id)
    ended = await store.get_conversation(conversation.public_id)
    assert ended is not None
    return ended


# ── wrap-up ─────────────────────────────────────────────────────────────────


async def test_one_call_titles_and_summarizes(store: RegistryStore, llm: _ScriptedLLM) -> None:
    conversation = await _ended_conversation(store)
    llm.replies = [
        '```json\n{"title": "Pasta timer and fettuccine cooking time.", '
        '"summary": "Set a 12-minute timer; fettuccine takes about 11 minutes."}\n```'
    ]

    result = await conversation_wrapup.wrap_up_conversation(store, {}, conversation)

    assert result.status == "model"
    saved = await store.get_conversation(conversation.public_id)
    assert saved is not None
    # Six words at most, no trailing period.
    assert (saved.title, saved.title_source) == ("Pasta timer and fettuccine cooking time", "auto")
    assert saved.summary == "Set a 12-minute timer; fettuccine takes about 11 minutes."
    assert saved.wrapped_up_at is not None
    ((messages, system_prompt),) = llm.calls
    assert "Person: set a timer for pasta" in messages[0]["content"]
    assert "voice assistant" in system_prompt


async def test_a_manual_title_is_kept(store: RegistryStore, llm: _ScriptedLLM) -> None:
    conversation = await _ended_conversation(store)
    await store.rename_conversation(conversation.public_id, "Dinner")
    llm.replies = ['{"title": "Something else", "summary": "Pasta."}']
    await conversation_wrapup.wrap_up_conversation(
        store,
        {},
        await store.get_conversation(conversation.public_id),  # type: ignore[arg-type]
    )
    saved = await store.get_conversation(conversation.public_id)
    assert saved is not None and (saved.title, saved.summary) == ("Dinner", "Pasta.")


async def test_short_conversations_get_a_fallback_title_without_a_call(
    store: RegistryStore, llm: _ScriptedLLM
) -> None:
    conversation = await _ended_conversation(
        store, turns=[("user", "what time is it right now please"), ("assistant", "3 PM.")]
    )
    result = await conversation_wrapup.wrap_up_conversation(store, {}, conversation)
    assert result.status == "skipped" and llm.calls == []
    saved = await store.get_conversation(conversation.public_id)
    assert saved is not None
    assert (saved.title, saved.title_source, saved.summary) == (
        "what time is it right now please",
        "fallback",
        None,
    )
    assert saved.wrapped_up_at is not None


async def test_settings_off_means_no_call(store: RegistryStore, llm: _ScriptedLLM) -> None:
    conversation = await _ended_conversation(store)
    await store.set_agent_conversation_settings(
        "dev-1", {"auto_title": False, "summarize_conversations": False}
    )
    result = await conversation_wrapup.wrap_up_conversation(store, {}, conversation)
    assert result.status == "skipped" and llm.calls == []


async def test_titles_only_when_summaries_are_off(store: RegistryStore, llm: _ScriptedLLM) -> None:
    conversation = await _ended_conversation(store)
    await store.set_agent_conversation_settings("dev-1", {"summarize_conversations": False})
    llm.replies = ['{"title": "Pasta timer", "summary": "Should not be kept."}']
    await conversation_wrapup.wrap_up_conversation(store, {}, conversation)
    saved = await store.get_conversation(conversation.public_id)
    assert saved is not None and (saved.title, saved.summary) == ("Pasta timer", None)


async def test_a_failed_call_falls_back_retries_once_then_gives_up(
    store: RegistryStore, llm: _ScriptedLLM
) -> None:
    conversation = await _ended_conversation(store)
    llm.replies = [RuntimeError("404 removed"), "not json at all"]

    first = await conversation_wrapup.sweep(store, {})
    saved = await store.get_conversation(conversation.public_id)
    assert first["failed"] == 1
    assert saved is not None and saved.title == "set a timer for pasta"
    assert (saved.title_source, saved.wrap_up_attempts, saved.wrapped_up_at) == (
        "fallback",
        1,
        None,
    )

    second = await conversation_wrapup.sweep(store, {})
    assert second["failed"] == 1
    saved = await store.get_conversation(conversation.public_id)
    assert saved is not None and saved.wrapped_up_at is not None  # gave up
    third = await conversation_wrapup.sweep(store, {})
    assert "failed" not in third and len(llm.calls) == 2


async def test_transcripts_are_summarized_as_recordings(
    store: RegistryStore, llm: _ScriptedLLM
) -> None:
    await store.get_or_create_agent("rec-1", kind=AgentKind.XIAOZHI)
    persona = await store.get_persona_for_device("rec-1")
    conversation = await store.transcript_conversation("rec-1", "20260917T100000Z-aa11", persona)
    for role, content in [
        ("transcript", "let's move the demo to Friday"),
        ("image", "[image:w.jpg] a whiteboard with a schedule"),
        ("transcript", "agreed, Friday at noon"),
    ]:
        await store.append_history("rec-1", role, content, conversation_id=conversation.id)
    await store.end_transcript_conversation("20260917T100000Z-aa11")
    llm.replies = ['{"title": "Demo moved to Friday", "summary": "The demo moves to Friday noon."}']

    await conversation_wrapup.sweep(store, {})

    ((messages, system_prompt),) = llm.calls
    assert "room microphone" in system_prompt
    assert "Photo: a whiteboard with a schedule" in messages[0]["content"]
    saved = await store.get_conversation("20260917T100000Z-aa11")
    assert saved is not None and saved.title == "Demo moved to Friday"


# ── idle sweep ──────────────────────────────────────────────────────────────


async def test_the_sweep_ends_conversations_past_their_own_idle_gap(
    store: RegistryStore, llm: _ScriptedLLM
) -> None:
    for device_id in ("dev-quick", "dev-slow"):
        await store.get_or_create_agent(device_id, kind=AgentKind.XIAOZHI)
        persona = await store.get_persona_for_device(device_id)
        conversation = await store.open_conversation(device_id, persona=persona, idle_minutes=30)
        assert conversation is not None
        await store.append_history(device_id, "user", "hi", conversation_id=conversation.id)
        async with store._sessions() as session:
            await session.execute(
                update(Conversation)
                .where(Conversation.id == conversation.id)
                .values(last_turn_at=_utc(minutes_ago=10))
            )
            await session.commit()
    await store.set_agent_conversation_settings("dev-quick", {"conversation_idle_minutes": 5})

    counts = await conversation_wrapup.sweep(store, {})

    assert counts["ended"] == 1
    (quick,) = await store.list_conversations("dev-quick")
    (slow,) = await store.list_conversations("dev-slow")
    assert quick.ended_at is not None and quick.wrapped_up_at is not None  # 1 turn: fallback
    assert slow.ended_at is None


# ── remembered conversations ────────────────────────────────────────────────


def test_the_memory_note_lists_summaries_newest_first_in_local_time() -> None:
    from zoneinfo import ZoneInfo

    older = Conversation(
        title="Weather for the ride",
        summary="Rain after 3 PM.",
        ended_at=datetime(2026, 9, 15, 13, 2),
    )
    newer = Conversation(
        title=None, summary="Set a pasta timer.", ended_at=datetime(2026, 9, 16, 23, 17)
    )
    note = memory_note([newer, older], ZoneInfo("America/New_York"))
    assert note.splitlines() == [
        "Earlier conversations with you (most recent first):",
        "- Sep 16, 7:17 PM — Untitled: Set a pasta timer.",
        "- Sep 15, 9:02 AM — Weather for the ride: Rain after 3 PM.",
    ]


async def test_memory_needs_summaries_and_a_nonzero_count(store: RegistryStore) -> None:
    conversation = await _ended_conversation(store)
    await store.save_wrap_up(
        conversation.id,
        title="Pasta",
        title_source="auto",
        summary="Fettuccine takes 11 minutes.",
        done=True,
        max_attempts=2,
    )
    persona = await store.get_persona_for_device("dev-1")
    agent = await store.get_agent("dev-1")
    on = effective_settings(persona, agent)
    note = await memory_note_for_turn(store, {}, "dev-1", None, on)
    assert "Pasta: Fettuccine takes 11 minutes." in note
    # Its own conversation is never in the note.
    assert await memory_note_for_turn(store, {}, "dev-1", conversation.id, on) == ""

    assert agent is not None
    agent.remember_conversations = 0
    assert (
        await memory_note_for_turn(store, {}, "dev-1", None, effective_settings(persona, agent))
        == ""
    )
    agent.remember_conversations = 3
    agent.summarize_conversations = False
    assert (
        await memory_note_for_turn(store, {}, "dev-1", None, effective_settings(persona, agent))
        == ""
    )


async def test_a_new_conversation_remembers_the_last_one(
    store: RegistryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store.get_or_create_agent("robot-1", kind=AgentKind.MCP)
    prompts: list[str] = []

    class _LLM:
        async def complete_with_tools(
            self, messages: Any, tools: Any, run: Any, system_prompt: str = ""
        ) -> str:
            prompts.append(system_prompt)
            return "ok"

    monkeypatch.setattr(agent_turn, "get_provider", lambda *a, **k: _LLM())
    await agent_turn.run_turn(store, {}, "robot-1", "I'm vegetarian")
    (conversation,) = await store.list_conversations("robot-1")
    await store.end_open_conversations("robot-1")
    await store.save_wrap_up(
        conversation.id,
        title="Diet",
        title_source="auto",
        summary="The user is vegetarian.",
        done=True,
        max_attempts=2,
    )

    await agent_turn.run_turn(store, {}, "robot-1", "suggest dinner")

    assert "Earlier conversations" not in prompts[0]
    assert "Diet: The user is vegetarian." in prompts[1]
