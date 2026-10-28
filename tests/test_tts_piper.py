"""Piper voice system: picked in the persona editor, downloaded on first use, spoken as PCM."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.error import HTTPError

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_hub.dashboard import app as dashboard_app
from agent_hub.dashboard import persona_options
from agent_hub.providers import tts as tts_pkg
from agent_hub.providers.tts.piper import PiperTTSProvider
from agent_hub.registry.store import RegistryStore


class _FakeVoice:
    """Stands in for piper.PiperVoice: two sentences of silence at 22050 Hz."""

    config = SimpleNamespace(sample_rate=22050)

    def __init__(self) -> None:
        self.length_scales: list[float | None] = []

    def synthesize(self, text: str, syn_config: Any = None) -> list[Any]:
        self.length_scales.append(syn_config.length_scale)
        return [SimpleNamespace(audio_int16_bytes=b"\x00\x00" * 10)] * 2


@pytest.fixture
def fake_piper(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the network download and ONNX load; record what was asked for."""
    import piper
    import piper.download_voices

    seen: dict[str, Any] = {"downloads": [], "voice": _FakeVoice()}

    def _download(voice: str, download_dir: Path, force_redownload: bool = False) -> None:
        seen["downloads"].append(voice)
        if voice == "en_US-nobody-medium":
            raise HTTPError("url", 404, "Not Found", {}, None)  # type: ignore[arg-type]
        (download_dir / f"{voice}.onnx").write_bytes(b"model")
        (download_dir / f"{voice}.onnx.json").write_text("{}")

    monkeypatch.setattr(piper.download_voices, "download_voice", _download)
    monkeypatch.setattr(piper.PiperVoice, "load", staticmethod(lambda path: seen["voice"]))
    return seen


async def test_a_voice_downloads_once_then_speaks(tmp_path: Path, fake_piper: Any) -> None:
    provider = PiperTTSProvider(data_dir=tmp_path, speed=2.0)
    pcm, rate = await provider.synthesize_pcm("Hello. There.", voice="en_GB-alba-medium")
    await provider.synthesize_pcm("Again.", voice="en_GB-alba-medium")
    assert rate == 22050
    assert len(pcm) == 40  # both sentence chunks, joined
    assert fake_piper["downloads"] == ["en_GB-alba-medium"]
    assert (tmp_path / "en_GB-alba-medium.onnx").exists()
    assert fake_piper["voice"].length_scales[0] == 0.5  # speed 2 = half the length
    wav = await provider.synthesize("Hi.")
    assert wav[:4] == b"RIFF"


async def test_a_bad_voice_raises_value_error_so_the_default_takes_over(
    tmp_path: Path, fake_piper: Any
) -> None:
    provider = PiperTTSProvider(data_dir=tmp_path)
    with pytest.raises(ValueError, match="not a Piper voice id"):
        await provider.synthesize_pcm("Hi.", voice="Luna")
    with pytest.raises(ValueError, match="No published Piper voice"):
        await provider.synthesize_pcm("Hi.", voice="en_US-nobody-medium")
    # A failed download leaves nothing behind that looks like a voice.
    assert list(tmp_path.glob("*.onnx*")) == []


def test_get_provider_reads_config_and_ignores_a_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tts_pkg, "_cache", {})
    config = {"tts": {"piper": {"voice": "en_US-amy-medium", "data_dir": "/tmp/v"}}}
    provider = tts_pkg.get_provider("piper", config, "anything")
    assert isinstance(provider, PiperTTSProvider)
    assert provider._voice == "en_US-amy-medium"
    assert provider._data_dir == Path("/tmp/v")
    assert tts_pkg.get_provider("piper", config) is provider


def test_piper_voices_are_offered_and_checked() -> None:
    assert "piper" in persona_options.TTS_PROVIDERS
    assert persona_options.voices_for("piper") == persona_options.PIPER_VOICES
    assert persona_options.voice_problem("piper", "en_US-lessac-medium") is None
    # Not on the list but published (another language): accepted, downloads on use.
    assert persona_options.voice_problem("piper", "de_DE-thorsten-medium") is None
    assert persona_options.voice_problem("piper", "Luna")
    assert persona_options.tts_model_problem("piper", "some-model")


async def test_editor_lists_piper_voices_and_saves_one(store: RegistryStore) -> None:
    app = FastAPI()
    app.include_router(dashboard_app.make_router(store, {}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        voices = await c.get("/dashboard/persona-voices", params={"tts_provider": "piper"})
        bad = await c.post(
            "/dashboard/personas/hub-default",
            data={"tts_provider": "piper", "tts_voice": "en-US-AriaNeural"},
        )
        await c.post(
            "/dashboard/personas/hub-default",
            data={"tts_provider": "piper", "tts_voice": "en_GB-alba-medium"},
        )
    assert "en_US-lessac-medium" in voices.text and "Luna" not in voices.text
    assert bad.status_code == 400 and "not a Piper voice id" in bad.text
    persona = await store.get_persona_by_name("hub-default")
    assert persona is not None
    assert (persona.tts_provider, persona.tts_voice) == ("piper", "en_GB-alba-medium")
