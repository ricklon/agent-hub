"""A deterministic fake LLM provider for the page-agent harness.

The real registry (``agent_hub.providers.llm.get_provider``) only knows
``openai``. For plumbing tests — does a tool call route through the bridge,
does history persist, is the system prompt assembled from tool defs — swap in a
:class:`ScriptedLLM` whose tool calls and reply are fixed.

The text turn lives in ``agent_hub.server.agent_turn``, which binds
``get_provider`` at import time, so patch it there::

    from tests.harness import ScriptedLLM, install_scripted_llm

    install_scripted_llm(
        monkeypatch,
        ScriptedLLM(tool_calls=[("get_screen", {})], reply="Disk is at 92%."),
    )
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any

import pytest

from agent_hub.providers.llm import LLMProvider

_ToolCall = tuple[str, dict[str, Any]]


class ScriptedLLM(LLMProvider):
    """An LLM provider that executes fixed tool calls, then returns fixed text."""

    def __init__(
        self,
        *,
        reply: str = "ok",
        tool_calls: Sequence[_ToolCall] = (),
    ) -> None:
        """Create the provider.

        Args:
            reply: The final text returned from every completion.
            tool_calls: ``(name, arguments)`` pairs passed to the executor, in
                order, before the reply — mimicking an LLM that decided to call
                those tools.
        """
        self._reply = reply
        self._tool_calls = list(tool_calls)
        self.executed: list[_ToolCall] = []
        self.results: list[str] = []

    async def complete(
        self,
        messages: list[dict[str, str]],
        system_prompt: str = "",
    ) -> str:
        return self._reply

    async def complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        tool_executor: Callable[[str, dict[str, Any]], Awaitable[str]],
        system_prompt: str = "",
    ) -> str:
        for name, args in self._tool_calls:
            self.results.append(await tool_executor(name, args))
            self.executed.append((name, args))
        return self._reply

    def stream(
        self,
        messages: list[dict[str, str]],
        system_prompt: str = "",
    ) -> AsyncIterator[str]:
        async def _gen() -> AsyncIterator[str]:
            yield self._reply

        return _gen()


def install_scripted_llm(monkeypatch: pytest.MonkeyPatch, llm: ScriptedLLM) -> ScriptedLLM:
    """Route every text turn at ``llm``. Returns it.

    Patches both places a turn can get a provider: ``agent_turn`` (the shared
    loop behind ``/page-agent/ask``, the robot console and the dashboard) and
    ``page_agent``'s own page-voice path, which still builds its own.
    """
    monkeypatch.setattr(
        "agent_hub.server.agent_turn.get_provider",
        lambda *_a, **_k: llm,
    )
    monkeypatch.setattr(
        "agent_hub.providers.llm.get_provider",
        lambda *_a, **_k: llm,
    )
    return llm


__all__ = ["ScriptedLLM", "install_scripted_llm"]
