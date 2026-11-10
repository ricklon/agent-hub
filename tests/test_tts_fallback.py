"""A cloud voice that fails is replaced by Piper, and not retried every sentence."""

from __future__ import annotations

from typing import Any

import pytest

from agent_hub.providers import tts as tts_pkg
from agent_hub.providers.tts import TTSProvider
from agent_hub.providers.tts.fallback import FallbackTTS


class _Voice(TTSProvider):
    def __init__(self, label: str, fail: Exception | None = None) -> None:
        self.label = label
        self.fail = fail
        self.calls = 0

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        self.calls += 1
        if self.fail:
            raise self.fail
        return self.label.encode()

    async def synthesize_pcm(self, text: str, voice: str | None = None) -> tuple[bytes, int]:
        self.calls += 1
        if self.fail:
            raise self.fail
        return self.label.encode(), 16000


async def test_a_working_cloud_voice_is_used() -> None:
    cloud, local = _Voice("cloud"), _Voice("local")
    voice = FallbackTTS(cloud, lambda: local, "edge")
    assert (await voice.synthesize_pcm("hi"))[0] == b"cloud"
    assert local.calls == 0


async def test_a_failing_cloud_voice_falls_back_and_is_skipped_for_a_while() -> None:
    cloud, local = _Voice("cloud", RuntimeError("403")), _Voice("local")
    voice = FallbackTTS(cloud, lambda: local, "edge")
    assert (await voice.synthesize_pcm("one"))[0] == b"local"
    assert (await voice.synthesize_pcm("two"))[0] == b"local"
    assert (await voice.synthesize("three")) == b"local"
    assert cloud.calls == 1  # not asked again during the cooldown


async def test_the_cloud_is_tried_again_after_the_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_hub.providers.tts.fallback as fb

    cloud, local = _Voice("cloud", RuntimeError("403")), _Voice("local")
    voice = FallbackTTS(cloud, lambda: local, "edge")
    await voice.synthesize_pcm("one")
    cloud.fail = None
    monkeypatch.setattr(fb, "COOLDOWN_S", 0.0)
    voice._skip_until = 0.0
    assert (await voice.synthesize_pcm("two"))[0] == b"cloud"


async def test_a_bad_voice_id_still_reaches_the_callers_own_handling() -> None:
    cloud, local = _Voice("cloud", ValueError("bad voice")), _Voice("local")
    voice = FallbackTTS(cloud, lambda: local, "edge")
    with pytest.raises(ValueError, match="bad voice"):
        await voice.synthesize_pcm("hi", voice="nope")
    assert local.calls == 0


def test_only_cloud_voices_get_a_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tts_pkg, "_cache", {})
    monkeypatch.setattr(tts_pkg, "_fallbacks", {})
    config: dict[str, Any] = {"tts": {}}
    assert isinstance(tts_pkg.get_provider_with_fallback("edge", config), FallbackTTS)
    assert not isinstance(tts_pkg.get_provider_with_fallback("piper", config), FallbackTTS)
    assert not isinstance(tts_pkg.get_provider_with_fallback("kitten", config), FallbackTTS)


def test_the_fallback_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tts_pkg, "_cache", {})
    monkeypatch.setattr(tts_pkg, "_fallbacks", {})
    plain = tts_pkg.get_provider_with_fallback("edge", {"tts": {"fallback": False}})
    assert not isinstance(plain, FallbackTTS)


def test_the_wrapper_is_rebuilt_when_the_primary_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tts_pkg, "_cache", {})
    monkeypatch.setattr(tts_pkg, "_fallbacks", {})
    first = tts_pkg.get_provider_with_fallback("edge", {"tts": {}})
    assert tts_pkg.get_provider_with_fallback("edge", {"tts": {}}) is first
    monkeypatch.setitem(tts_pkg._cache, "edge", _Voice("other"))
    assert tts_pkg.get_provider_with_fallback("edge", {"tts": {}}) is not first
