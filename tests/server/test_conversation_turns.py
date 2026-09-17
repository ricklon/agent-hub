"""Turn paths use the current conversation, and save every message they add."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import update

from agent_hub.registry.models import AgentKind, Conversation, Persona
from agent_hub.registry.store import RegistryStore
from agent_hub.server import agent_turn, ws_session


class _RecordingLLM:
    def __init__(self) -> None:
        self.sent: list[list[dict[str, Any]]] = []

    async def complete_with_tools(
        self, messages: list[dict[str, Any]], *args: Any, **kwargs: Any
    ) -> str:
        self.sent.append(messages)
        return f"answer {len(self.sent)}"


async def test_dashboard_ask_remembers_within_a_conversation_but_not_across_the_gap(
    store: RegistryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store.get_or_create_agent("robot-1", kind=AgentKind.MCP)
    llm = _RecordingLLM()
    monkeypatch.setattr(agent_turn, "get_provider", lambda *a, **k: llm)

    await agent_turn.run_turn(store, {}, "robot-1", "my name is Ada")
    await agent_turn.run_turn(store, {}, "robot-1", "what is my name?")
    # The second turn saw the first: same conversation.
    assert [m["content"] for m in llm.sent[1]] == ["my name is Ada", "answer 1", "what is my name?"]

    (conversation,) = await store.list_conversations("robot-1")
    async with store._sessions() as session:
        await session.execute(
            update(Conversation)
            .where(Conversation.id == conversation.id)
            .values(last_turn_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2))
        )
        await session.commit()

    await agent_turn.run_turn(store, {}, "robot-1", "hello again")
    # After the idle gap: a new conversation, starting fresh.
    assert [m["content"] for m in llm.sent[2]] == ["hello again"]
    newest, oldest = await store.list_conversations("robot-1")
    assert (newest.turn_count, oldest.turn_count) == (1, 2)
    assert oldest.ended_at is not None and newest.ended_at is None


async def test_a_full_context_window_no_longer_loses_the_users_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The voice turn trimmed the session list in place, shifting the index the
    caller saves new messages from; once full, only the reply was saved."""

    class _LLM:
        async def stream_with_tools(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
            yield "fine"

        async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
            yield "fine"

    async def fake_stream_to_speech(websocket: Any, deltas: Any, *args: Any, **kwargs: Any):
        return "".join([d async for d in deltas]), 0, 0

    monkeypatch.setattr(ws_session, "get_llm", lambda *a, **k: _LLM())
    monkeypatch.setattr(ws_session, "_stream_reply_to_speech", fake_stream_to_speech)
    persona = Persona(
        name="p",
        llm_provider="openai",
        tts_provider="edge",
        asr_provider="moonshine",
        system_prompt="",
        memory_window=1,
        server_skills=None,
        mcp_tools_allowlist=None,
    )
    history = [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
    ]
    prev_len = len(history)  # what the session saves from

    await ws_session._run_llm_turn(
        object(),  # type: ignore[arg-type]
        "how are you?",
        "session-1",
        persona,
        history,
        {},
        None,
        "",
        False,
        memory_window=1,
    )

    assert [m["content"] for m in history[prev_len:]] == ["how are you?", "fine"]
