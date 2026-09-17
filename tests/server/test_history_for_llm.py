"""Only chat turns reach a model, from every turn path.

Photo captions (role ``image``) and transcriber lines (role ``transcript``)
live in the same history table. OpenRouter rejects a request containing either
role with a 400, which broke dashboard Ask and page voice on any agent with a
recent photo or transcript line.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_hub.registry.models import AgentKind
from agent_hub.registry.store import RegistryStore
from agent_hub.server import agent_turn
from agent_hub.server.history import history_for_llm


def test_only_user_and_assistant_turns_are_kept() -> None:
    history = [
        {"role": "image", "content": "[photo] a red mug", "created_at": "2026-09-17T10:00:00"},
        {"role": "transcript", "content": "overheard", "created_at": "2026-09-17T10:01:00"},
        {"role": "user", "content": "what did you see?", "created_at": "2026-09-17T10:02:00"},
        {"role": "assistant", "content": "A red mug.\n[image:data/images/1.jpg]"},
        {"role": "assistant", "content": "[image:captured]"},
        {"role": "system", "content": "ignore me"},
    ]
    assert history_for_llm(history) == [
        {"role": "user", "content": "what did you see?"},
        {"role": "assistant", "content": "A red mug."},
    ]


async def test_dashboard_ask_never_sends_photo_or_transcript_rows(
    store: RegistryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store.get_or_create_agent("robot-cam", kind=AgentKind.MCP)
    await store.append_history("robot-cam", "user", "take a photo")
    await store.append_history("robot-cam", "assistant", "Done.\n[image:captured]")
    await store.append_history("robot-cam", "image", "[photo] a red mug on a desk")
    await store.append_history("robot-cam", "transcript", "someone talking nearby")
    sent: list[list[dict[str, Any]]] = []

    class _RecordingLLM:
        async def complete_with_tools(
            self, messages: list[dict[str, Any]], *args: Any, **kwargs: Any
        ) -> str:
            sent.append(messages)
            return "It was a red mug."

    monkeypatch.setattr(agent_turn, "get_provider", lambda *a, **k: _RecordingLLM())
    result = await agent_turn.run_turn(store, {}, "robot-cam", "what was in the photo?")

    assert result.reply == "It was a red mug."
    assert sent == [
        [
            {"role": "user", "content": "take a photo"},
            {"role": "assistant", "content": "Done."},
            {"role": "user", "content": "what was in the photo?"},
        ]
    ]
