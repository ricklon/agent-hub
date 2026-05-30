"""OpenAI Chat Completions LLM provider."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, cast

from openai import AsyncOpenAI

from agent_hub.providers.llm import LLMProvider


class OpenAILLMProvider(LLMProvider):
    """LLM completions via the OpenAI Chat API.

    Also works with any OpenAI-compatible endpoint (Ollama, LM Studio,
    etc.) by setting base_url to the local server address.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        base_url: str | None = None,
    ) -> None:
        """Create an OpenAILLMProvider.

        Args:
            api_key: OpenAI API key (or any string for local endpoints
                that don't check auth).
            model: Chat model name.
            base_url: Override API base URL (e.g. 'http://localhost:11434/v1'
                for Ollama).
        """
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=60.0)
        self._model = model

    def _build_messages(
        self, messages: list[dict[str, str]], system_prompt: str
    ) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        if system_prompt:
            result.append({"role": "system", "content": system_prompt})
        result.extend(messages)
        return result

    async def complete(
        self,
        messages: list[dict[str, str]],
        system_prompt: str = "",
    ) -> str:
        """Generate a single chat completion.

        Args:
            messages: Chat history.
            system_prompt: Injected as the first system message.

        Returns:
            Model response content string.
        """
        completions = cast(Any, self._client.chat.completions)
        resp = await completions.create(
            model=self._model,
            messages=self._build_messages(messages, system_prompt),
        )
        return (resp.choices[0].message.content or "").strip()

    async def complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        tool_executor: Callable[[str, dict[str, Any]], Awaitable[str]],
        system_prompt: str = "",
        max_rounds: int = 5,
    ) -> str:
        working: list[dict[str, Any]] = list(self._build_messages(messages, system_prompt))
        completions = cast(Any, self._client.chat.completions)

        for _ in range(max_rounds):
            resp = await completions.create(
                model=self._model,
                messages=working,
                tools=tools,
                tool_choice="auto",
            )
            if not resp.choices:
                return ""
            msg = resp.choices[0].message

            if not msg.tool_calls:
                return (msg.content or "").strip()

            working.append(msg.model_dump(exclude_unset=True))

            for tc in msg.tool_calls:
                if tc is None or tc.function is None:
                    continue
                args = json.loads(tc.function.arguments or "{}")
                result = await tool_executor(tc.function.name or "", args)
                # Image results (data URLs) need multimodal content blocks
                if isinstance(result, str) and result.startswith("data:"):
                    content: Any = [{"type": "image_url", "image_url": {"url": result}}]
                else:
                    content = result
                working.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id or "",
                        "content": content,
                    }
                )

        # Exhausted rounds — final call without tools
        resp = await completions.create(
            model=self._model,
            messages=working,
        )
        if not resp.choices:
            return ""
        return (resp.choices[0].message.content or "").strip()

    async def stream(
        self,
        messages: list[dict[str, str]],
        system_prompt: str = "",
    ) -> AsyncIterator[str]:
        """Stream chat completion tokens.

        Args:
            messages: Chat history.
            system_prompt: Injected as the first system message.

        Yields:
            Text delta strings.
        """
        completions = cast(Any, self._client.chat.completions)
        stream = await completions.create(
            model=self._model,
            messages=self._build_messages(messages, system_prompt),
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content or ""
            if delta:
                yield delta
