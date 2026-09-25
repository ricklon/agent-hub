"""Exercise browser voice turns without audio models or external providers."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from fastapi import WebSocket

from agent_hub.config import Settings
from agent_hub.registry.models import AgentKind
from agent_hub.registry.store import RegistryStore
from agent_hub.server.page_agent import make_router


@pytest.mark.parametrize("result", ["Hello there", "", "tts_failure"])
async def test_voice_turn_includes_current_question_and_finishes(
    store: RegistryStore, monkeypatch: pytest.MonkeyPatch, result: str
) -> None:
    """A turn includes its new utterance and always signals completion or error."""
    from agent_hub.providers import asr, llm, tts
    from agent_hub.server import audio

    class Vad:
        segment_ms = 10

        def __init__(self, **kwargs: object) -> None:
            pass

        def push(self, pcm: bytes) -> bool:
            return True

        def take_pcm(self) -> bytes:
            return b"\x00\x00" * 160

    class Asr:
        async def transcribe(self, wav: bytes) -> SimpleNamespace:
            return SimpleNamespace(is_speech=True, text="computer how are you")

    class Llm:
        async def complete_with_tools(self, messages: list, *args: object, **kwargs: object) -> str:
            assert messages[-1] == {"role": "user", "content": "how are you"}
            return result

        async def stream_with_tools(
            self, messages: list, *args: object, **kwargs: object
        ) -> AsyncIterator[str]:
            assert messages[-1] == {"role": "user", "content": "how are you"}
            if result:
                yield result

    class Tts:
        async def synthesize_pcm(self, text: str, **kwargs: object) -> tuple[bytes, int]:
            if result == "tts_failure":
                raise RuntimeError("provider unavailable")
            return b"\x00\x00" * 2000, 16000

    monkeypatch.setattr(audio, "PcmSileroVAD", Vad)
    monkeypatch.setattr(asr, "get_provider", lambda *args: Asr())
    monkeypatch.setattr(llm, "get_provider", lambda *args, **kwargs: Llm())
    monkeypatch.setattr(tts, "get_provider", lambda *args: Tts())
    await store.get_or_create_agent("voice-test", kind=AgentKind.PAGE)
    token = await store.issue_websocket_token("voice-test")
    events = iter(
        [
            {"type": "websocket.connect"},
            {"type": "websocket.receive", "bytes": b"\x00\x00" * 160},
            {"type": "websocket.disconnect", "code": 1000},
        ]
    )
    sent: list[dict] = []

    async def receive() -> dict:
        event = next(events)
        if event["type"] == "websocket.disconnect":
            # The turn runs as its own task; hang up once it has finished.
            for _ in range(200):
                texts = [json.loads(e["text"]) for e in sent if "text" in e]
                if any(m["type"] == "error" or m.get("state") == "stop" for m in texts):
                    break
                await asyncio.sleep(0.01)
        return event

    async def send(message: dict) -> None:
        sent.append(message)

    socket = WebSocket(
        {
            "type": "websocket",
            "path": "/page-agent/voice",
            "headers": [],
            "query_string": f"device_id=voice-test&token={token}".encode(),
        },
        receive,
        send,
    )
    router = make_router(store, Settings(), {})
    route = next(route for route in router.routes if route.path == "/page-agent/voice")
    await route.endpoint(socket)
    messages = [json.loads(event["text"]) for event in sent if "text" in event]
    assert any(message["type"] == "thinking" for message in messages)
    if result == "Hello there":
        assert messages[-1] == {"type": "tts", "state": "stop", "interrupted": False}
        assert sum(len(event.get("bytes", b"")) for event in sent) == 4000
    elif result == "tts_failure":
        # One sentence failing to synthesize is reported, and the turn still ends.
        assert any(message["type"] == "tts_error" for message in messages)
        assert messages[-1]["state"] == "stop"
    else:
        assert messages[-1]["type"] == "error"


@pytest.mark.parametrize("held_bytes", [2 * 16000, 2 * 1600])
async def test_push_to_talk_answers_exactly_the_held_audio(
    store: RegistryStore, monkeypatch: pytest.MonkeyPatch, held_bytes: int
) -> None:
    """Held audio is the utterance: no VAD, no wake word; a slip of a press is ignored."""
    from agent_hub.providers import asr, llm, tts
    from agent_hub.server import audio

    vad_pushes: list[int] = []
    heard_wavs: list[int] = []
    asked: list[str] = []
    offered: list[set[str]] = []

    class Vad:
        segment_ms = 10

        def __init__(self, **kwargs: object) -> None:
            pass

        def push(self, pcm: bytes) -> bool:
            vad_pushes.append(len(pcm))
            return True

        def take_pcm(self) -> bytes:
            return b""

        def reset(self) -> None:
            pass

    class Asr:
        async def transcribe(self, wav: bytes) -> SimpleNamespace:
            heard_wavs.append(len(wav))
            return SimpleNamespace(is_speech=True, text="I have had a fever for two days")

    class Llm:
        async def stream_with_tools(
            self, messages: list, *args: object, **kwargs: object
        ) -> AsyncIterator[str]:
            asked.append(messages[-1]["content"])
            tools = args[0] if args and isinstance(args[0], list) else kwargs.get("tools", [])
            offered.append({t["function"]["name"] for t in tools})  # type: ignore[union-attr]
            yield "How high has it been?"

        async def complete_with_tools(self, messages: list, *args: object, **kwargs: object) -> str:
            asked.append(messages[-1]["content"])
            return "How high has it been?"

    class Tts:
        async def synthesize_pcm(self, text: str, **kwargs: object) -> tuple[bytes, int]:
            return b"\x00\x00" * 100, 16000

    monkeypatch.setattr(audio, "PcmSileroVAD", Vad)
    monkeypatch.setattr(asr, "get_provider", lambda *args: Asr())
    monkeypatch.setattr(llm, "get_provider", lambda *args, **kwargs: Llm())
    monkeypatch.setattr(tts, "get_provider", lambda *args: Tts())
    await store.get_or_create_agent("ptt-test", kind=AgentKind.PAGE)
    token = await store.issue_websocket_token("ptt-test")
    half = held_bytes // 2
    events = iter(
        [
            {"type": "websocket.connect"},
            {
                "type": "websocket.receive",
                "text": json.dumps({"type": "talk_mode", "mode": "push"}),
            },
            # Room chatter before anyone presses: never heard.
            {"type": "websocket.receive", "bytes": b"\x01\x00" * 8000},
            {"type": "websocket.receive", "text": json.dumps({"type": "ptt", "state": "down"})},
            {"type": "websocket.receive", "bytes": b"\x02\x00" * (half // 2)},
            {"type": "websocket.receive", "bytes": b"\x02\x00" * (half // 2)},
            {"type": "websocket.receive", "text": json.dumps({"type": "ptt", "state": "up"})},
            {"type": "websocket.disconnect", "code": 1000},
        ]
    )
    sent: list[dict] = []

    async def receive() -> dict:
        event = next(events)
        if event["type"] == "websocket.disconnect":
            for _ in range(200):
                texts = [json.loads(e["text"]) for e in sent if "text" in e]
                if any(m["type"] in ("error", "heard") or m.get("state") == "stop" for m in texts):
                    break
                await asyncio.sleep(0.01)
        return event

    async def send(message: dict) -> None:
        sent.append(message)

    socket = WebSocket(
        {
            "type": "websocket",
            "path": "/page-agent/voice",
            "headers": [],
            "query_string": f"device_id=ptt-test&token={token}".encode(),
        },
        receive,
        send,
    )
    router = make_router(store, Settings(), {})
    route = next(route for route in router.routes if route.path == "/page-agent/voice")
    await route.endpoint(socket)
    messages = [json.loads(event["text"]) for event in sent if "text" in event]

    assert vad_pushes == [], "push-to-talk never consults the VAD"
    if held_bytes >= 2 * 16000 * 300 // 1000:
        assert heard_wavs == [44 + held_bytes], "only the held audio is transcribed"
        assert asked == ["I have had a fever for two days"], "answered with no wake word"
        # The persona has not ticked fetch_page, so the voice session does not offer it.
        assert offered and "fetch_page" not in offered[0] and "web_search" in offered[0]
        assert messages[-1] == {"type": "tts", "state": "stop", "interrupted": False}
    else:
        assert heard_wavs == [] and asked == []
        assert {"type": "heard", "text": "", "reason": "too short"} in messages
