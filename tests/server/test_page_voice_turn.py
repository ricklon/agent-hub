"""Exercise browser voice turns without audio models or external providers."""

from __future__ import annotations

import json
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
        return next(events)

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
        assert messages[-1] == {"type": "tts", "state": "stop"}
        assert sum(len(event.get("bytes", b"")) for event in sent) == 4000
    else:
        assert messages[-1]["type"] == "error"
