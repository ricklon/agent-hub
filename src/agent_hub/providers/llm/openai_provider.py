"""OpenAI Chat Completions LLM provider."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, cast

from openai import AsyncOpenAI

from agent_hub import spend
from agent_hub.providers.llm import LLMProvider

# Endpoints known to report token usage. Local servers (Ollama, LM Studio)
# often reject the extra fields, so usage reporting is opt-in by host rather
# than sent blindly to whatever base_url is configured.
_USAGE_CAPABLE_HOSTS = ("openrouter.ai", "api.openai.com")


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
        # base_url=None means api.openai.com.
        self._usage_capable = base_url is None or any(
            host in base_url for host in _USAGE_CAPABLE_HOSTS
        )
        # OpenRouter only returns a real cost when asked for it.
        self._cost_capable = base_url is not None and "openrouter.ai" in base_url

    def _usage_kwargs(self, *, stream: bool) -> dict[str, Any]:
        """Extra request fields that make the endpoint report token usage."""
        if not self._usage_capable:
            return {}
        kwargs: dict[str, Any] = {}
        if stream:
            # Without this a streamed response carries no usage block at all.
            kwargs["stream_options"] = {"include_usage": True}
        if self._cost_capable:
            kwargs["extra_body"] = {"usage": {"include": True}}
        return kwargs

    async def _meter(self, usage: Any) -> None:
        """Record one call's usage. Falls back to estimation when unreported."""
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        # OpenRouter puts the real charge on usage.cost; everyone else omits it
        # and we fall back to the configured price table.
        raw_cost = getattr(usage, "cost", None)
        cost = float(raw_cost) if raw_cost is not None else None
        await spend.record(self._model, prompt_tokens, completion_tokens, cost)

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
        await spend.guard()
        completions = cast(Any, self._client.chat.completions)
        resp = await completions.create(
            model=self._model,
            messages=self._build_messages(messages, system_prompt),
            **self._usage_kwargs(stream=False),
        )
        await self._meter(getattr(resp, "usage", None))
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
            await spend.guard()
            resp = await completions.create(
                model=self._model,
                messages=working,
                tools=tools,
                tool_choice="auto",
                **self._usage_kwargs(stream=False),
            )
            # Each tool round is a separate billed call, so meter before any
            # early return below.
            await self._meter(getattr(resp, "usage", None))
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
                # Image results (data URLs) need multimodal content blocks.
                # Some providers reject image_url in a tool-role message, so
                # send it as a user message alongside the tool acknowledgement.
                if isinstance(result, str) and result.startswith("data:"):
                    working.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id or "",
                            "content": "Image captured and attached.",
                        }
                    )
                    content: Any = [
                        {"type": "text", "text": "Here is the image you requested:"},
                        {"type": "image_url", "image_url": {"url": result}},
                    ]
                    working.append({"role": "user", "content": content})
                else:
                    working.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id or "",
                            "content": result,
                        }
                    )

        # Exhausted rounds — final call without tools
        await spend.guard()
        resp = await completions.create(
            model=self._model,
            messages=working,
            **self._usage_kwargs(stream=False),
        )
        await self._meter(getattr(resp, "usage", None))
        if not resp.choices:
            return ""
        return (resp.choices[0].message.content or "").strip()

    async def stream_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        tool_executor: Callable[[str, dict[str, Any]], Awaitable[str]],
        system_prompt: str = "",
        max_rounds: int = 5,
    ) -> AsyncIterator[str]:
        """Stream the final assistant response while preserving tool calls."""
        working: list[dict[str, Any]] = list(self._build_messages(messages, system_prompt))
        completions = cast(Any, self._client.chat.completions)

        for _ in range(max_rounds):
            await spend.guard()
            stream = await completions.create(
                model=self._model,
                messages=working,
                tools=tools,
                tool_choice="auto",
                stream=True,
                **self._usage_kwargs(stream=True),
            )

            content_parts: list[str] = []
            tool_calls: dict[int, dict[str, str]] = {}
            async for chunk in stream:
                # The usage block arrives in its own trailing chunk, which
                # carries no choices — check it before skipping those.
                if getattr(chunk, "usage", None):
                    await self._meter(chunk.usage)
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                delta_content = delta.content or ""
                if delta_content:
                    content_parts.append(delta_content)
                    yield delta_content

                for tc in delta.tool_calls or []:
                    index = int(tc.index or 0)
                    slot = tool_calls.setdefault(
                        index,
                        {"id": "", "type": "function", "name": "", "arguments": ""},
                    )
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.type:
                        slot["type"] = tc.type
                    if tc.function:
                        if tc.function.name:
                            slot["name"] += tc.function.name
                        if tc.function.arguments:
                            slot["arguments"] += tc.function.arguments

            if content_parts and not tool_calls:
                return
            if not tool_calls:
                return

            assistant_tool_calls: list[dict[str, Any]] = []
            for index in sorted(tool_calls):
                call = tool_calls[index]
                assistant_tool_calls.append(
                    {
                        "id": call["id"],
                        "type": call["type"] or "function",
                        "function": {
                            "name": call["name"],
                            "arguments": call["arguments"],
                        },
                    }
                )
            working.append(
                {
                    "role": "assistant",
                    "content": "".join(content_parts) or None,
                    "tool_calls": assistant_tool_calls,
                }
            )

            for call in assistant_tool_calls:
                fn = cast(dict[str, Any], call["function"])
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = await tool_executor(str(fn.get("name") or ""), args)
                if isinstance(result, str) and result.startswith("data:"):
                    working.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": "Image captured and attached.",
                        }
                    )
                    tool_content: Any = [
                        {"type": "text", "text": "Here is the image you requested:"},
                        {"type": "image_url", "image_url": {"url": result}},
                    ]
                    working.append({"role": "user", "content": tool_content})
                else:
                    working.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": result,
                        }
                    )

        async for delta in self.stream(cast(list[dict[str, str]], working), system_prompt=""):
            yield delta

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
        await spend.guard()
        completions = cast(Any, self._client.chat.completions)
        stream = await completions.create(
            model=self._model,
            messages=self._build_messages(messages, system_prompt),
            stream=True,
            **self._usage_kwargs(stream=True),
        )
        async for chunk in stream:
            # The trailing usage chunk has no choices; indexing [0] would raise.
            if getattr(chunk, "usage", None):
                await self._meter(chunk.usage)
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content or ""
            if delta:
                yield delta
