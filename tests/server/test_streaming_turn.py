"""Tests for streaming voice-turn helpers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from agent_hub.providers.llm import LLMProvider
from agent_hub.server.ws_session import _DelayedTurnCue, _take_speakable_chunks


class _FallbackLLM(LLMProvider):
    async def complete(self, messages: list[dict[str, str]], system_prompt: str = "") -> str:
        return "unused"

    async def complete_with_tools(self, *args, **kwargs) -> str:
        return "Tool-aware fallback."

    async def stream(
        self, messages: list[dict[str, str]], system_prompt: str = ""
    ) -> AsyncIterator[str]:
        yield "unused"


def test_take_speakable_chunks_keeps_incomplete_tail() -> None:
    chunks, tail = _take_speakable_chunks("First sentence. Second sentence")

    assert chunks == ["First sentence."]
    assert tail == "Second sentence"


async def test_stream_with_tools_default_yields_complete_with_tools_result() -> None:
    llm = _FallbackLLM()
    chunks = [
        chunk
        async for chunk in llm.stream_with_tools(
            [{"role": "user", "content": "hi"}],
            [],
            lambda _name, _args: None,  # type: ignore[arg-type]
        )
    ]

    assert chunks == ["Tool-aware fallback."]


async def test_delayed_turn_cue_fires_after_delay() -> None:
    calls: list[str] = []

    async def speak_cue() -> None:
        calls.append("cue")

    cue = _DelayedTurnCue(0.001, speak_cue)
    cue.start()
    await asyncio.sleep(0.01)
    await cue.close()

    assert calls == ["cue"]


async def test_delayed_turn_cue_cancels_before_delay() -> None:
    calls: list[str] = []

    async def speak_cue() -> None:
        calls.append("cue")

    cue = _DelayedTurnCue(60.0, speak_cue)
    cue.start()
    await cue.settle_before_reply()

    assert calls == []


async def test_delayed_turn_cue_respects_specific_cue() -> None:
    calls: list[str] = []

    async def speak_cue() -> None:
        calls.append("cue")

    cue = _DelayedTurnCue(0.001, speak_cue)
    cue.start()
    cue.mark_handled()
    await asyncio.sleep(0.01)
    await cue.close()

    assert calls == []
