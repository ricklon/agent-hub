"""Tests that the OpenAI provider meters spend and honours the limit."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agent_hub import spend
from agent_hub.providers.llm.openai_provider import OpenAILLMProvider
from agent_hub.registry.store import RegistryStore
from agent_hub.spend import SpendLimitExceeded


class _FakeCompletions:
    """Stands in for client.chat.completions, recording the kwargs it sees."""

    def __init__(self, response: Any = None, chunks: list[Any] | None = None) -> None:
        self._response = response
        self._chunks = chunks or []
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if kwargs.get("stream"):

            async def _iter():
                for chunk in self._chunks:
                    yield chunk

            return _iter()
        return self._response


def _install(provider: OpenAILLMProvider, completions: _FakeCompletions) -> None:
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))


def _message(content: str) -> Any:
    return SimpleNamespace(content=content, tool_calls=None)


async def _store(tmp_path) -> RegistryStore:
    store = RegistryStore(db_path=tmp_path / "registry.db")
    await store.initialize()
    return store


@pytest.fixture(autouse=True)
def _reset_spend():
    spend.reset()
    yield
    spend.reset()


class TestMetering:
    async def test_complete_records_the_reported_cost(self, tmp_path):
        store = await _store(tmp_path)
        spend.configure(store, {})
        provider = OpenAILLMProvider(
            api_key="k", model="m", base_url="https://openrouter.ai/api/v1"
        )
        completions = _FakeCompletions(
            response=SimpleNamespace(
                choices=[SimpleNamespace(message=_message("hi"))],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=4, cost=0.002),
            )
        )
        _install(provider, completions)

        assert await provider.complete([{"role": "user", "content": "hi"}]) == "hi"

        summary = await store.llm_spend_summary()
        assert summary["cost_usd"] == pytest.approx(0.002)
        assert summary["prompt_tokens"] == 10
        assert summary["estimated_calls"] == 0

    async def test_streaming_usage_chunk_is_metered_and_not_yielded(self, tmp_path):
        store = await _store(tmp_path)
        spend.configure(store, {})
        provider = OpenAILLMProvider(
            api_key="k", model="m", base_url="https://openrouter.ai/api/v1"
        )
        # The final usage chunk carries no choices at all — indexing choices[0]
        # on it would raise, and it must not be emitted as text.
        chunks = [
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="he"))], usage=None
            ),
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="llo"))], usage=None
            ),
            SimpleNamespace(
                choices=[],
                usage=SimpleNamespace(prompt_tokens=7, completion_tokens=2, cost=0.004),
            ),
        ]
        _install(provider, _FakeCompletions(chunks=chunks))

        out = [delta async for delta in provider.stream([{"role": "user", "content": "hi"}])]

        assert "".join(out) == "hello"
        summary = await store.llm_spend_summary()
        assert summary["cost_usd"] == pytest.approx(0.004)
        assert summary["calls"] == 1


class TestUsageRequestFields:
    async def test_openrouter_is_asked_for_usage_and_cost(self, tmp_path):
        spend.configure(await _store(tmp_path), {})
        provider = OpenAILLMProvider(
            api_key="k", model="m", base_url="https://openrouter.ai/api/v1"
        )
        completions = _FakeCompletions(
            response=SimpleNamespace(choices=[SimpleNamespace(message=_message("x"))], usage=None)
        )
        _install(provider, completions)

        await provider.complete([{"role": "user", "content": "hi"}])

        assert completions.calls[0]["extra_body"] == {"usage": {"include": True}}

    async def test_local_endpoints_get_no_extra_fields(self, tmp_path):
        """Ollama and friends reject unknown request fields, so send none."""
        spend.configure(await _store(tmp_path), {})
        provider = OpenAILLMProvider(api_key="k", model="m", base_url="http://localhost:11434/v1")
        completions = _FakeCompletions(
            response=SimpleNamespace(choices=[SimpleNamespace(message=_message("x"))], usage=None)
        )
        _install(provider, completions)

        await provider.complete([{"role": "user", "content": "hi"}])

        assert "extra_body" not in completions.calls[0]
        assert "stream_options" not in completions.calls[0]

    async def test_streaming_requests_usage_in_the_stream(self, tmp_path):
        spend.configure(await _store(tmp_path), {})
        provider = OpenAILLMProvider(
            api_key="k", model="m", base_url="https://openrouter.ai/api/v1"
        )
        completions = _FakeCompletions(chunks=[])
        _install(provider, completions)

        [_ async for _ in provider.stream([{"role": "user", "content": "hi"}])]

        assert completions.calls[0]["stream_options"] == {"include_usage": True}


class TestLimitEnforcement:
    async def test_provider_refuses_to_call_the_api_once_capped(self, tmp_path):
        store = await _store(tmp_path)
        spend.configure(store, {"llm": {"spend": {"total_limit_usd": 0.01}}})
        await spend.record("m", 1, 1, cost_usd=0.02)

        provider = OpenAILLMProvider(
            api_key="k", model="m", base_url="https://openrouter.ai/api/v1"
        )
        completions = _FakeCompletions(
            response=SimpleNamespace(choices=[SimpleNamespace(message=_message("x"))], usage=None)
        )
        _install(provider, completions)

        with pytest.raises(SpendLimitExceeded):
            await provider.complete([{"role": "user", "content": "hi"}])

        # The point of guarding before the request: no billable call went out.
        assert completions.calls == []

    async def test_streaming_is_capped_before_the_request(self, tmp_path):
        store = await _store(tmp_path)
        spend.configure(store, {"llm": {"spend": {"daily_limit_usd": 0.01}}})
        await spend.record("m", 1, 1, cost_usd=0.05)

        provider = OpenAILLMProvider(
            api_key="k", model="m", base_url="https://openrouter.ai/api/v1"
        )
        completions = _FakeCompletions(chunks=[])
        _install(provider, completions)

        with pytest.raises(SpendLimitExceeded):
            [_ async for _ in provider.stream([{"role": "user", "content": "hi"}])]

        assert completions.calls == []


class TestToolArgumentParsing:
    """Malformed tool arguments are recoverable, not fatal.

    A cheap model that emits almost-JSON used to kill the whole turn with a
    JSONDecodeError, losing the user's question. The loop now hands the parse
    error back so the model can correct itself on the next round.
    """

    def test_valid_arguments_parse(self) -> None:
        from agent_hub.providers.llm.openai_provider import _parse_tool_arguments

        assert _parse_tool_arguments('{"speed": 5}') == ({"speed": 5}, None)
        assert _parse_tool_arguments(None) == ({}, None)
        assert _parse_tool_arguments("") == ({}, None)

    def test_malformed_arguments_return_an_explanation(self) -> None:
        from agent_hub.providers.llm.openai_provider import _parse_tool_arguments

        args, error = _parse_tool_arguments('{"speed": 5, "secs"')
        assert args == {}
        assert error is not None and "valid JSON" in error

    def test_non_object_arguments_are_rejected(self) -> None:
        from agent_hub.providers.llm.openai_provider import _parse_tool_arguments

        args, error = _parse_tool_arguments("[1, 2]")
        assert args == {}
        assert error is not None and "JSON object" in error
