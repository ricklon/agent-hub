"""Tests for TTS behavior in the WebSocket session."""

from __future__ import annotations

from typing import Any

from agent_hub.registry.models import Persona
from agent_hub.server import ws_session


class _FakeWebSocket:
    def __init__(self) -> None:
        self.text_messages: list[str] = []
        self.binary_messages: list[bytes] = []

    async def send_text(self, text: str) -> None:
        self.text_messages.append(text)

    async def send_bytes(self, data: bytes) -> None:
        self.binary_messages.append(data)


class _FakeTTS:
    def __init__(self) -> None:
        self.voices: list[str | None] = []

    async def synthesize_pcm(self, text: str, voice: str | None = None) -> tuple[bytes, int]:
        self.voices.append(voice)
        if voice == "en-GB-RyanNeutral":
            raise ValueError("Invalid voice 'en-GB-RyanNeutral'.")
        return b"\x00\x00", ws_session.SERVER_TTS_AUDIO_PARAMS.sample_rate


class _FakeOpusEncoder:
    def __init__(self, sample_rate: int, *, frame_duration_ms: int) -> None:
        self.sample_rate = sample_rate
        self.frame_duration_ms = frame_duration_ms

    def encode(self, pcm_bytes: bytes) -> list[bytes]:
        return [b"opus-packet"]


class _FakeRateController:
    PRE_BUFFER_COUNT = 10

    def __init__(self, *, frame_duration_ms: int) -> None:
        self.frame_duration_ms = frame_duration_ms
        self._queue: list[bytes] = []

    def add_audio(self, packet: bytes) -> None:
        self._queue.append(packet)

    def start(self, send_bytes: Any) -> None:
        pass

    async def wait_until_done(self) -> None:
        pass

    def stop(self) -> None:
        pass


async def test_speak_falls_back_to_default_voice_on_invalid_persona_voice(monkeypatch) -> None:
    """A typo in persona.tts_voice should not abort the whole voice turn."""
    tts = _FakeTTS()
    monkeypatch.setattr(ws_session, "get_tts", lambda provider, config: tts)
    monkeypatch.setattr(ws_session, "OpusEncoder", _FakeOpusEncoder)
    monkeypatch.setattr(ws_session, "AudioRateController", _FakeRateController)

    persona = Persona(
        name="grumpy-pirate",
        llm_provider="openai",
        tts_provider="edge",
        tts_voice="en-GB-RyanNeutral",
        asr_provider="funasr",
    )
    websocket = _FakeWebSocket()

    await ws_session._speak(websocket, "Ahoy.", persona, {}, "session-1")

    assert tts.voices == ["en-GB-RyanNeutral", None]
    assert websocket.binary_messages == [b"opus-packet"]
    assert '"state": "start"' in websocket.text_messages[0]
    assert '"state": "stop"' in websocket.text_messages[-1]
