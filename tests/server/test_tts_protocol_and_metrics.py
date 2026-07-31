"""Tests for the TTS wire protocol and turn latency metrics.

Covers two bugs found against a real device on the hosted deployment:
  #46 replies never reached the device screen — no sentence_start was sent
  #47 LLM and TTS latency were always logged as the same number
"""

from __future__ import annotations

import json

import pytest

from agent_hub.registry.models import Persona
from agent_hub.server import ws_session


class _FakeTTS:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def synthesize_pcm(self, text: str, voice: str | None = None) -> tuple[bytes, int]:
        self.calls.append(text)
        return (b"\x00\x00" * 160, 16000)


class _FakeWebSocket:
    def __init__(self) -> None:
        self.text_messages: list[str] = []
        self.binary_messages: list[bytes] = []

    async def send_text(self, data: str) -> None:
        self.text_messages.append(data)

    async def send_bytes(self, data: bytes) -> None:
        self.binary_messages.append(data)


@pytest.fixture
def persona() -> Persona:
    return Persona(
        name="test",
        llm_provider="openai",
        tts_provider="edge",
        tts_voice=None,
        asr_provider="moonshine",
    )


@pytest.fixture(autouse=True)
def _stub_tts(monkeypatch) -> _FakeTTS:
    tts = _FakeTTS()
    monkeypatch.setattr(ws_session, "get_tts", lambda *_a, **_k: tts)
    monkeypatch.setattr(ws_session, "OpusEncoder", lambda *a, **k: _NullEncoder())
    return tts


class _NullEncoder:
    def encode(self, pcm: bytes) -> list[bytes]:
        return [b"opus"]


def _messages(ws: _FakeWebSocket) -> list[dict]:
    return [json.loads(m) for m in ws.text_messages]


class TestSentenceStart:
    """xiaozhi firmware displays reply text only from `sentence_start`.

    Its OnIncomingJson handler ignores `text` on the `start` message, so
    without this the device speaks with a blank screen.
    """

    async def test_a_sentence_start_carries_the_text(self, persona):
        ws = _FakeWebSocket()

        await ws_session._speak(ws, "Hello there.", persona, {}, "s1")

        starts = [m for m in _messages(ws) if m.get("state") == "sentence_start"]
        assert [m["text"] for m in starts] == ["Hello there."]

    async def test_every_segment_gets_its_own_sentence_start(self, persona):
        """`start` is sent once per turn, so per-segment text must not ride on it."""
        ws = _FakeWebSocket()

        await ws_session._speak_segment(
            ws, "First.", persona, {}, "s1", send_start=True, send_stop=False
        )
        await ws_session._speak_segment(
            ws, "Second.", persona, {}, "s1", send_start=False, send_stop=False
        )

        msgs = _messages(ws)
        assert [m["text"] for m in msgs if m.get("state") == "sentence_start"] == [
            "First.",
            "Second.",
        ]
        # Only the first segment opens the turn.
        assert len([m for m in msgs if m.get("state") == "start"]) == 1

    async def test_sentence_start_follows_start(self, persona):
        ws = _FakeWebSocket()

        await ws_session._speak(ws, "Ordered.", persona, {}, "s1")

        states = [m.get("state") for m in _messages(ws)]
        assert states.index("start") < states.index("sentence_start")
        assert states[-1] == "stop"


class TestTurnMetrics:
    async def test_first_token_and_synthesis_are_measured_separately(self, persona):
        """They used to be clamped to the same elapsed window and matched exactly."""
        ws = _FakeWebSocket()

        async def _deltas():
            yield "Hello "
            yield "world."

        reply, tts_ms, first_delta_ms = await ws_session._stream_reply_to_speech(
            ws, _deltas(), persona, {}, "s1"
        )

        assert reply == "Hello world."
        # Distinct measurements: neither is derived from the other.
        assert isinstance(tts_ms, int)
        assert isinstance(first_delta_ms, int)
        assert first_delta_ms >= 0

    async def test_no_deltas_reports_zero_synthesis(self, persona):
        """A silent turn must report tts_ms 0 rather than a clamped elapsed time.

        The old max() clamp hid exactly this signal, which is what let #46 go
        unnoticed: TTS looked busy every turn even when nothing was spoken.
        """
        ws = _FakeWebSocket()

        async def _deltas():
            return
            yield  # pragma: no cover

        reply, tts_ms, _ = await ws_session._stream_reply_to_speech(
            ws, _deltas(), persona, {}, "s1"
        )

        assert reply == ""
        assert tts_ms == 0
