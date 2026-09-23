"""A busy model falls back to another, and a failed turn says why in words.

Free OpenRouter models share an upstream pool and get rate-limited (429) for
minutes at a time. OpenRouter can move to the next model within the same
request when given a ``models`` list; a free persona must never fall back to
a paid model and start costing money.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from agent_hub import spend
from agent_hub.providers.llm import get_provider, model_list, resolved_model
from agent_hub.providers.llm.model_check import failure_message
from agent_hub.providers.llm.openai_provider import OpenAILLMProvider, route_models
from agent_hub.registry.models import AgentKind
from agent_hub.registry.store import RegistryStore
from agent_hub.server import agent_turn, session_state
from agent_hub.server.agent_turn import TurnError, run_turn

_OPENROUTER = "https://openrouter.ai/api/v1"


def _rate_limited() -> openai.RateLimitError:
    request = httpx.Request("POST", f"{_OPENROUTER}/chat/completions")
    return openai.RateLimitError(
        "Error code: 429",
        response=httpx.Response(429, request=request),
        body={"message": "Provider returned error", "code": 429},
    )


class _Completions:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def _install(provider: OpenAILLMProvider, completions: _Completions) -> None:
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _reset_spend() -> Any:
    spend.reset()
    yield
    spend.reset()


def test_a_free_model_only_falls_back_to_free_models() -> None:
    fallbacks = ["google/gemma-4-31b-it", "qwen/qwen3.8-27b:free", "a/b:free", "c/d:free"]
    assert route_models("ling:free", fallbacks) == [
        "ling:free",
        "qwen/qwen3.8-27b:free",
        "a/b:free",
    ]


def test_a_paid_model_may_fall_back_to_anything_without_repeating_itself() -> None:
    fallbacks = ["paid/x", "paid/x", "free/y:free", "paid/z"]
    assert route_models("paid/x", fallbacks) == ["paid/x", "free/y:free", "paid/z"]


def test_fallbacks_come_from_yaml_lists_or_comma_separated_env() -> None:
    assert model_list(["a", " b "]) == ["a", "b"]
    assert model_list("a:free, b:free,") == ["a:free", "b:free"]
    assert model_list(None) == []


async def test_openrouter_requests_carry_the_fallback_route(tmp_path: Any) -> None:
    store = RegistryStore(db_path=tmp_path / "registry.db")
    await store.initialize()
    spend.configure(store, {})
    provider = OpenAILLMProvider(
        api_key="k", model="ling:free", base_url=_OPENROUTER, fallback_models=["gemma:free"]
    )
    completions = _Completions(
        response=SimpleNamespace(
            model="gemma:free",
            choices=[SimpleNamespace(message=SimpleNamespace(content="hi", tool_calls=None))],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=1, cost=0.0),
        )
    )
    _install(provider, completions)

    assert await provider.complete_with_tools([{"role": "user", "content": "hi"}], [], _noop) == (
        "hi"
    )

    body = completions.calls[0]["extra_body"]
    assert body["models"] == ["ling:free", "gemma:free"]
    assert body["usage"] == {"include": True}  # usage reporting is kept
    # Spend is charged to the model that actually answered.
    by_model = {row["model"] for row in await store.llm_spend_by_model()}
    assert by_model == {"gemma:free"}


def test_no_route_without_fallbacks_or_off_openrouter() -> None:
    plain = OpenAILLMProvider(api_key="k", model="m", base_url=_OPENROUTER)
    assert "models" not in plain._request_kwargs(stream=False).get("extra_body", {})
    local = OpenAILLMProvider(
        api_key="k", model="m", base_url="http://localhost:11434/v1", fallback_models=["x"]
    )
    assert "extra_body" not in local._request_kwargs(stream=False)


def test_get_provider_reads_configured_fallbacks() -> None:
    config = {
        "llm": {
            "openai": {
                "api_key": "k",
                "base_url": _OPENROUTER,
                "model": "default/model",
                "fallback_models": "busy/fallback:free",
            }
        }
    }
    provider = get_provider("openai", config, model_override="test/fallback-read:free")
    assert isinstance(provider, OpenAILLMProvider)
    assert provider._route == ["test/fallback-read:free", "busy/fallback:free"]
    assert resolved_model(config, "openai", None) == "default/model"


def test_a_rate_limit_is_described_in_words_with_advice() -> None:
    message = failure_message("ling:free", _rate_limited())
    assert message == (
        "The model ling:free is unavailable: rate-limited by the provider. "
        "Try again in a moment, or pick another model for this persona."
    )
    assert "Provider returned error" not in message


async def test_a_rate_limited_turn_says_why_and_is_recorded(
    store: RegistryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store.get_or_create_agent("page-busy", kind=AgentKind.PAGE)
    persona = await store.get_persona_for_device("page-busy")
    assert persona is not None

    class _Busy:
        async def complete_with_tools(self, *args: Any, **kwargs: Any) -> str:
            raise _rate_limited()

    monkeypatch.setattr(agent_turn, "get_provider", lambda *a, **k: _Busy())
    with pytest.raises(TurnError) as raised:
        await run_turn(store, {}, "page-busy", "hello")

    model = resolved_model({}, persona.llm_provider, persona.llm_model or None)
    assert str(raised.value).startswith(f"The model {model} is unavailable: rate-limited")
    recorded = session_state.get_llm_error("page-busy")
    assert recorded is not None and recorded["model"] == model


async def _noop(name: str, args: dict[str, Any]) -> str:
    return ""
