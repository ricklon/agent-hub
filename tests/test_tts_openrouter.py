"""The OpenRouter voice system: request shape, PCM rate, errors, spend."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from agent_hub import spend
from agent_hub.providers import tts as tts_pkg
from agent_hub.providers.tts.openrouter import OpenRouterTTSProvider
from agent_hub.registry.models import Persona
from agent_hub.server.persona_voice import synthesize_persona


def _provider(handler: Any, **kwargs: Any) -> OpenRouterTTSProvider:
    return OpenRouterTTSProvider(
        api_key=kwargs.pop("api_key", "sk-or-test"),
        model="openai/gpt-4o-mini-tts-2025-12-15",
        voice="alloy",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


async def test_requests_pcm_and_reads_the_rate_from_the_content_type() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            content=b"\x01\x00\x02\x00",
            headers={"content-type": "audio/pcm;rate=22050;channels=1"},
        )

    pcm, rate = await _provider(handler, speed=1.1).synthesize_pcm("Hello there.", voice="nova")
    assert (pcm, rate) == (b"\x01\x00\x02\x00", 22050)
    request = seen[0]
    assert str(request.url) == "https://openrouter.ai/api/v1/audio/speech"
    assert request.headers["authorization"] == "Bearer sk-or-test"
    assert json.loads(request.content) == {
        "model": "openai/gpt-4o-mini-tts-2025-12-15",
        "input": "Hello there.",
        "voice": "nova",
        "response_format": "pcm",
        "speed": 1.1,
    }


async def test_default_voice_and_rate() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, content=b"\x00\x00", headers={"content-type": "audio/pcm"})

    assert await _provider(handler).synthesize_pcm("Hi.") == (b"\x00\x00", 24000)
    assert bodies[0]["voice"] == "alloy"
    assert "speed" not in bodies[0]


async def test_a_rejected_voice_falls_back_to_the_default_with_a_notice() -> None:
    voices: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        voice = json.loads(request.content)["voice"]
        voices.append(voice)
        if voice == "nonsense":
            return httpx.Response(400, json={"error": {"message": "Invalid voice: nonsense"}})
        return httpx.Response(200, content=b"\x00\x00", headers={"content-type": "audio/pcm"})

    persona = Persona(name="p", tts_provider="openrouter", tts_voice="nonsense")
    assert await synthesize_persona(_provider(handler), "Hello.", persona, "dev-1") == (
        b"\x00\x00",
        24000,
    )
    assert voices == ["nonsense", "alloy"]


async def test_other_errors_are_reported_not_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"error": {"message": "Insufficient credits"}})

    with pytest.raises(RuntimeError, match="OpenRouter TTS 402: Insufficient credits"):
        await _provider(handler).synthesize_pcm("Hello.", voice="nova")


async def test_no_key_fails_before_any_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request without a key")

    with pytest.raises(RuntimeError, match="needs an API key"):
        await _provider(handler, api_key="").synthesize_pcm("Hello.")


async def test_each_sentence_is_guarded_and_recorded_as_spend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Any]] = []

    async def guard() -> None:
        calls.append(("guard", None))

    async def record(model: str, prompt: int, completion: int, cost: float | None) -> None:
        calls.append(("record", (model, cost)))

    monkeypatch.setattr(spend, "guard", guard)
    monkeypatch.setattr(spend, "record", record)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\x00\x00", headers={"content-type": "audio/pcm"})

    await _provider(handler, price_per_million_chars=15.0).synthesize_pcm("x" * 1000)
    await _provider(handler).synthesize_pcm("Hi.")
    assert calls == [
        ("guard", None),
        ("record", ("openai/gpt-4o-mini-tts-2025-12-15", 0.015)),
        ("guard", None),
        ("record", ("openai/gpt-4o-mini-tts-2025-12-15", None)),
    ]


def test_registry_reuses_the_openrouter_llm_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tts_pkg, "_cache", {})
    config = {
        "llm": {"openai": {"api_key": "sk-or-llm", "base_url": "https://openrouter.ai/api/v1"}},
        "tts": {"openrouter": {"model": "hexgrad/kokoro-82m", "voice": "af_heart"}},
    }
    provider = tts_pkg.get_provider("openrouter", config)
    assert isinstance(provider, OpenRouterTTSProvider)
    assert provider._api_key == "sk-or-llm"
    assert provider._model == "hexgrad/kokoro-82m"


def test_registry_does_not_send_a_non_openrouter_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tts_pkg, "_cache", {})
    config = {"llm": {"openai": {"api_key": "sk-openai", "base_url": None}}}
    provider = tts_pkg.get_provider("openrouter", config)
    assert isinstance(provider, OpenRouterTTSProvider)
    assert provider._api_key == ""
