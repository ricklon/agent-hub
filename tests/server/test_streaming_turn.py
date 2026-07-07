"""Tests for streaming voice-turn helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator

from agent_hub.providers.llm import LLMProvider
from agent_hub.server.ws_session import _take_speakable_chunks


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
