"""LLM provider base class and factory."""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any


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

    async def stream_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        tool_executor: Callable[[str, dict[str, Any]], Awaitable[str]],
        system_prompt: str = "",
    ) -> AsyncIterator[str]:
        """Stream the final response after any required tool calls.

        Default implementation preserves compatibility for providers that do
        not support streaming tool loops yet.
        """
        yield await self.complete_with_tools(messages, tools, tool_executor, system_prompt)

    @abc.abstractmethod
    def stream(
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


_cache: dict[str, LLMProvider] = {}


def model_list(value: Any) -> list[str]:
    """A list of model ids from YAML (a list) or an env var (comma-separated)."""
    items = value if isinstance(value, list) else str(value or "").split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def resolved_model(config: dict[str, Any], provider: str, override: str | None) -> str:
    """The model a persona actually runs: its own, or the provider's default."""
    default = ((config.get("llm") or {}).get(provider or "openai") or {}).get("model", "")
    return override or str(default) or provider


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
        cache_key = f"{name}:{model}"
        if cache_key in _cache:
            return _cache[cache_key]
        provider: LLMProvider = OpenAILLMProvider(
            api_key=str(cfg.get("api_key", "")),
            model=model,
            base_url=cfg.get("base_url") or None,
            fallback_models=model_list(cfg.get("fallback_models")),
        )
        _cache[cache_key] = provider
        return provider
    raise ValueError(f"Unknown LLM provider: {name!r}")
