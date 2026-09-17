"""Model check: the verdicts, and what it reports for each kind of failure."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import openai
import pytest

from agent_hub.providers.llm import LLMProvider
from agent_hub.providers.llm.model_check import check_model, describe_error


def _api_error(
    cls: type[openai.APIStatusError], status: int, message: str
) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    return cls(message, response=httpx.Response(status, request=request), body={"message": message})


class ScriptedLLM(LLMProvider):
    """Plays one behaviour per turn: "tool", "no_tool", "empty", "slow", "hang", or an exception."""

    def __init__(self, script: list[Any], delay_s: float = 0.0) -> None:
        self.script = list(script)
        self.delay_s = delay_s

    async def complete(self, messages: list[dict[str, str]], system_prompt: str = "") -> str:
        raise NotImplementedError

    async def stream(  # type: ignore[override]
        self, messages: list[dict[str, str]], system_prompt: str = ""
    ) -> AsyncIterator[str]:
        raise NotImplementedError
        yield ""

    async def stream_with_tools(  # type: ignore[override]
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        tool_executor: Callable[[str, dict[str, Any]], Awaitable[str]],
        system_prompt: str = "",
    ) -> AsyncIterator[str]:
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        if step == "hang":
            await asyncio.sleep(5)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        asks_time = "time" in messages[-1]["content"]
        if step == "tool" and asks_time:
            await tool_executor("get_current_time", {})
        if step != "empty":
            yield "It is "
            yield "3:15 PM."


async def test_a_model_that_answers_and_calls_the_tool_is_usable() -> None:
    result = await check_model(ScriptedLLM(["tool", "tool", "tool"]), "good:free")
    assert result.verdict == "usable"
    assert [p.ok for p in result.probes] == [True, True, True]
    assert [p.tool_called for p in result.probes[1:]] == [True, True]
    assert result.probes[1].reply == "It is 3:15 PM."


async def test_a_model_that_never_calls_a_tool_is_unusable() -> None:
    result = await check_model(ScriptedLLM(["no_tool", "no_tool", "no_tool"]), "chatty:free")
    assert result.verdict == "unusable"
    assert "tool use is required" in result.reason


async def test_calling_the_tool_only_sometimes_is_unreliable() -> None:
    result = await check_model(ScriptedLLM(["tool", "tool", "no_tool"]), "flaky:free")
    assert result.verdict == "unreliable"
    assert result.reason == "1 of 3 turns failed: answered without calling the tool"


async def test_a_removed_model_is_unusable_and_says_why() -> None:
    gone = _api_error(openai.NotFoundError, 404, "This model is unavailable for free.")
    result = await check_model(ScriptedLLM([gone, gone, gone]), "minimax/minimax-m3:free")
    assert result.verdict == "unusable"
    assert result.reason.startswith("not available from the provider (removed or renamed)")
    assert "unavailable for free" in result.reason


async def test_an_empty_reply_counts_as_a_failure() -> None:
    result = await check_model(ScriptedLLM(["empty", "tool", "tool"]), "quiet:free")
    assert result.verdict == "unreliable"
    assert "returned an empty reply" in result.reason


async def test_a_model_slow_to_start_speaking_is_slow() -> None:
    result = await check_model(
        ScriptedLLM(["tool", "tool", "tool"], delay_s=0.05), "slow:free", slow_after_s=0.01
    )
    assert result.verdict == "slow"
    assert result.median_first_text_s is not None and result.median_first_text_s >= 0.05


async def test_a_turn_past_the_timeout_fails() -> None:
    result = await check_model(ScriptedLLM(["hang", "tool", "tool"]), "stuck:free", timeout_s=0.1)
    assert result.verdict == "unreliable"
    assert not result.probes[0].ok
    assert result.probes[0].problem == "no complete reply within 0.1s"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (
            _api_error(openai.RateLimitError, 429, "Provider returned error"),
            "rate-limited by the provider",
        ),
        (
            _api_error(openai.PermissionDeniedError, 403, "only on agentic harnesses"),
            "refused by the provider",
        ),
        (
            _api_error(openai.AuthenticationError, 401, "bad key"),
            "the provider rejected the API key",
        ),
    ],
)
def test_errors_are_described_plainly(exc: BaseException, expected: str) -> None:
    assert describe_error(exc).startswith(expected)
