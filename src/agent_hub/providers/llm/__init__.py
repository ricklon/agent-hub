"""LLM provider base class and factory."""

from __future__ import annotations

import abc
from typing import Any, AsyncIterator, Awaitable, Callable


class LLMProvider(abc.ABC):
    """Abstract base for language model providers."""

    @abc.abstractmethod
    async def complete(
        self,
        messages: list[dict[str, str]],
        system_prompt: str = "",
    ) -> str:
        """Generate a single completion.

        Args:
            messages: Chat history as [{"role": "user"|"assistant", "content": str}].
            system_prompt: Injected as a system message before the history.

        Returns:
            Model response text.
        """

    async def complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        tool_executor: Callable[[str, dict[str, Any]], Awaitable[str]],
        system_prompt: str = "",
    ) -> str:
        """Run the agentic loop: call LLM, execute tool calls, repeat until text.

        Default implementation ignores tools and falls back to complete().
        Providers that support tool calling should override this.

        Args:
            messages: Chat history.
            tools: OpenAI-format tool definitions.
            tool_executor: Coroutine called with (tool_name, args) → result str.
            system_prompt: Injected as system message.

        Returns:
            Final text response after all tool calls are resolved.
        """
        return await self.complete(messages, system_prompt)

    @abc.abstractmethod
    async def stream(
        self,
        messages: list[dict[str, str]],
        system_prompt: str = "",
    ) -> AsyncIterator[str]:
        """Stream completion tokens as they arrive.

        Args:
            messages: Chat history.
            system_prompt: Injected as a system message.

        Yields:
            Text delta strings.
        """


def get_provider(
    name: str,
    config: dict[str, Any],
    model_override: str | None = None,
) -> LLMProvider:
    """Instantiate an LLM provider by name from config.

    Args:
        name: Provider key matching .config.yaml llm.<name>.
        config: Full raw config dict.
        model_override: If set, overrides the model from config (e.g. persona.llm_model).

    Returns:
        Configured LLMProvider instance.

    Raises:
        ValueError: If the provider name is unknown.
    """
    llm_cfg: dict[str, Any] = config.get("llm", {})
    if name == "openai":
        from agent_hub.providers.llm.openai_provider import OpenAILLMProvider

        cfg = llm_cfg.get("openai", {})
        model = model_override or str(cfg.get("model", "gpt-4o-mini"))
        return OpenAILLMProvider(
            api_key=str(cfg.get("api_key", "")),
            model=model,
            base_url=cfg.get("base_url") or None,
        )
    raise ValueError(f"Unknown LLM provider: {name!r}")
