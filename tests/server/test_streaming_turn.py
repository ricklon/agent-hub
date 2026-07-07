"""Tests for streaming voice-turn helpers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from agent_hub.providers.llm import LLMProvider
from agent_hub.server.ws_session import (
    _asr_realtime_factor,
    _DelayedTurnCue,
    _history_for_llm,
    _strip_history_markers,
    _take_speakable_chunks,
)


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


def test_history_for_llm_skips_volatile_assistant_answers() -> None:
    history = [
        {"role": "user", "content": "what time is it?"},
        {
            "role": "assistant",
            "content": "It is 5:27 PM.\n[volatile-tools:get_current_time]",
        },
        {"role": "user", "content": "thanks"},
    ]

    assert _history_for_llm(history) == [
        {"role": "user", "content": "what time is it?"},
        {"role": "user", "content": "thanks"},
    ]


def test_strip_history_markers_removes_internal_metadata() -> None:
    assert (
        _strip_history_markers("It is raining.\n[volatile-tools:get_weather]") == "It is raining."
    )


def test_asr_realtime_factor_uses_audio_duration() -> None:
    assert _asr_realtime_factor(asr_ms=1500, frame_count=50, frame_duration_ms=20) == 1.5
