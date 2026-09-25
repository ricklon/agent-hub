"""The OpenRouter voice system: request shape, PCM rate, errors, spend."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from agent_hub import spend
from agent_hub.providers import openrouter_prices
from agent_hub.providers import tts as tts_pkg
from agent_hub.providers.tts import openrouter as openrouter_tts
from agent_hub.providers.tts.openrouter import OpenRouterTTSProvider
from agent_hub.registry.models import Persona
from agent_hub.server.persona_voice import synthesize_persona


def _provider(handler: Any, **kwargs: Any) -> OpenRouterTTSProvider:
    return OpenRouterTTSProvider(
        api_key=kwargs.pop("api_key", "sk-or-test"),
        model="google/gemini-3.8-flash-lite-tts",
        voice="Kore",
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

    pcm, rate = await _provider(handler, speed=1.1).synthesize_pcm("Hello there.", voice="Puck")
    assert (pcm, rate) == (b"\x01\x00\x02\x00", 22050)
    request = seen[0]
    assert str(request.url) == "https://openrouter.ai/api/v1/audio/speech"
    assert request.headers["authorization"] == "Bearer sk-or-test"
    assert json.loads(request.content) == {
        "model": "google/gemini-3.8-flash-lite-tts",
        "input": "Hello there.",
        "voice": "Puck",
        "response_format": "pcm",
        "speed": 1.1,
    }


async def test_default_voice_and_rate() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, content=b"\x00\x00", headers={"content-type": "audio/pcm"})

    assert await _provider(handler).synthesize_pcm("Hi.") == (b"\x00\x00", 24000)
    assert bodies[0]["voice"] == "Kore"
    assert "speed" not in bodies[0]


async def test_a_rejected_voice_falls_back_to_the_default_with_a_notice() -> None:
    voices: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        voice = json.loads(request.content)["voice"]
        voices.append(voice)
        if voice == "nonsense":
            # The real body: OpenRouter does not say the voice was the problem.
            return httpx.Response(
                400, json={"error": {"message": "Provider returned 400", "code": 400}}
            )
        return httpx.Response(200, content=b"\x00\x00", headers={"content-type": "audio/pcm"})

    persona = Persona(name="p", tts_provider="openrouter", tts_voice="nonsense")
    assert await synthesize_persona(_provider(handler), "Hello.", persona, "dev-1") == (
        b"\x00\x00",
        24000,
    )
    assert voices == ["nonsense", "Kore"]


async def test_a_bad_request_on_the_default_voice_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "Provider returned 400"}})

    with pytest.raises(RuntimeError, match="OpenRouter TTS 400"):
        await _provider(handler).synthesize_pcm("Hello.")


async def test_other_errors_are_reported_not_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"error": {"message": "Insufficient credits"}})

    with pytest.raises(RuntimeError, match="OpenRouter TTS 402: Insufficient credits"):
        await _provider(handler).synthesize_pcm("Hello.", voice="Puck")


async def test_no_key_fails_before_any_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request without a key")

    with pytest.raises(RuntimeError, match="needs an API key"):
        await _provider(handler, api_key="").synthesize_pcm("Hello.")


@pytest.fixture(autouse=True)
def _fresh_prices() -> Any:
    openrouter_prices.reset_cache()
    yield
    openrouter_prices.reset_cache()


_CATALOGUE = {
    "data": [
        {
            "id": "google/gemini-3.8-flash-lite-tts",
            "pricing": {"prompt": "0.0000005", "completion": "0.000006"},
        }
    ]
}


def _billing_handler(generation_lookups: list[str], *, billed: bool = True) -> Any:
    """Speech, catalogue and generation endpoints, like OpenRouter's."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/audio/speech"):
            # One second of 24 kHz mono int16.
            return httpx.Response(
                200,
                content=b"\x00\x00" * 24000,
                headers={"content-type": "audio/pcm;rate=24000", "x-generation-id": "gen-1"},
            )
        if path.endswith("/models"):
            assert request.url.params["output_modalities"] == "all"
            return httpx.Response(200, json=_CATALOGUE)
        if path.endswith("/generation"):
            generation_lookups.append(request.url.params["id"])
            if len(generation_lookups) == 1 or not billed:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            return httpx.Response(
                200,
                json={
                    "data": {
                        "total_cost": 0.0012125,
                        "native_tokens_prompt": 25,
                        "native_tokens_completion": 200,
                    }
                },
            )
        return httpx.Response(500)

    return handler


async def test_speech_is_estimated_per_token_then_settled_with_the_billed_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Any]] = []

    async def guard() -> None:
        calls.append(("guard", None))

    async def record(
        model: str, prompt: int, completion: int, cost: float | None, estimated: bool = False
    ) -> int:
        calls.append(("record", (model, prompt, completion, cost, estimated)))
        return 7

    async def settle(row_id: int, cost: float, prompt: int, completion: int) -> bool:
        calls.append(("settle", (row_id, cost, prompt, completion)))
        return True

    monkeypatch.setattr(spend, "guard", guard)
    monkeypatch.setattr(spend, "record", record)
    monkeypatch.setattr(spend, "settle", settle)
    lookups: list[str] = []
    provider = _provider(_billing_handler(lookups), settle_delays=(0, 0, 0))

    await provider.synthesize_pcm("x" * 100)
    await asyncio.gather(*openrouter_tts._settling)

    # 100 chars ≈ 25 tokens in; 1 s of audio ≈ 32 tokens out, at catalogue prices.
    estimate = 25 * 0.0000005 + 32 * 0.000006
    assert calls[0] == ("guard", None)
    assert calls[1][0] == "record"
    model, prompt, completion, cost, estimated = calls[1][1]
    assert (model, prompt, completion, estimated) == (
        "google/gemini-3.8-flash-lite-tts",
        25,
        32,
        True,
    )
    assert cost == pytest.approx(estimate)
    # The first lookup is too early (404); the second has the billed figures.
    assert lookups == ["gen-1", "gen-1"]
    assert calls[2] == ("settle", (7, 0.0012125, 25, 200))


async def test_without_a_billed_cost_the_estimate_stays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settled: list[Any] = []

    async def noop() -> None:
        return None

    async def record(*_args: Any, **_kw: Any) -> int:
        return 3

    async def settle(*args: Any) -> bool:
        settled.append(args)
        return True

    monkeypatch.setattr(spend, "guard", noop)
    monkeypatch.setattr(spend, "record", record)
    monkeypatch.setattr(spend, "settle", settle)
    lookups: list[str] = []
    provider = _provider(_billing_handler(lookups, billed=False), settle_delays=(0, 0))
    await provider.synthesize_pcm("Hi.")
    await asyncio.gather(*openrouter_tts._settling)
    assert lookups == ["gen-1", "gen-1"]
    assert settled == []


def test_audio_tokens_follow_the_length_of_speech() -> None:
    assert openrouter_tts.estimate_tokens("x" * 10, 2 * 24000 * 6, 24000) == (3, 192)


async def test_the_ledger_settles_an_estimated_row(store: Any) -> None:
    row = await store.record_llm_spend("m", 25, 32, 0.0002, True, device_id="page-1")
    assert await store.settle_llm_spend(row, 0.0012, 25, 200)
    summary = await store.llm_spend_summary()
    assert summary["cost_usd"] == pytest.approx(0.0012)
    assert summary["estimated_calls"] == 0
    assert not await store.settle_llm_spend(row + 99, 1.0, 0, 0)


async def test_speech_is_billed_to_the_agent_that_spoke(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str | None] = []

    async def record(*_args: Any, **_kw: Any) -> None:
        seen.append(spend._current_device.get())

    class _TTS:
        async def synthesize_pcm(self, text: str, voice: str | None = None) -> tuple[bytes, int]:
            await record()
            return b"\x00\x00", 24000

    persona = Persona(name="p", tts_provider="openrouter")
    await synthesize_persona(_TTS(), "Hello.", persona, "page-patient1")
    assert seen == ["page-patient1"]


def test_registry_reuses_the_openrouter_llm_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tts_pkg, "_cache", {})
    config = {
        "llm": {"openai": {"api_key": "sk-or-llm", "base_url": "https://openrouter.ai/api/v1"}},
        "tts": {"openrouter": {"model": "google/gemini-3.8-flash-tts", "voice": "Aoede"}},
    }
    provider = tts_pkg.get_provider("openrouter", config)
    assert isinstance(provider, OpenRouterTTSProvider)
    assert provider._api_key == "sk-or-llm"
    assert provider._model == "google/gemini-3.8-flash-tts"


def test_registry_does_not_send_a_non_openrouter_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tts_pkg, "_cache", {})
    config = {"llm": {"openai": {"api_key": "sk-openai", "base_url": None}}}
    provider = tts_pkg.get_provider("openrouter", config)
    assert isinstance(provider, OpenRouterTTSProvider)
    assert provider._api_key == ""
    assert provider._model == "google/gemini-3.8-flash-lite-tts"
