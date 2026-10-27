"""Idle voice sessions get a keepalive; reconnects don't replay the greeting.

xiaozhi firmware drops a session after 120 s with no incoming text or binary
frame and reconnects, redoing the MCP handshake. A Coglet S3 idling on its wake
word would do that every two minutes without a frame from the hub.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from agent_hub.registry.store import RegistryStore
from agent_hub.server import session_state, ws_session


class _FakeWebSocket:
    def __init__(self, device_id: str) -> None:
        self.headers = {"device-id": device_id}
        self.query_params: dict[str, str] = {}
        self.state = SimpleNamespace()
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent: list[Any] = []

    async def accept(self) -> None:
        pass

    async def close(self, code: int = 1000, reason: str = "") -> None:
        pass

    async def receive_text(self) -> str:
        return json.dumps({"type": "hello", "version": 1, "features": {"mcp": False}})

    async def receive(self) -> dict[str, Any]:
        return await self.inbox.get()

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        msgs = [json.loads(f) for f in self.sent if isinstance(f, str)]
        return [m for m in msgs if m.get("type") == kind]


class _FakeEncoder:
    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    def encode(self, pcm: bytes) -> list[bytes]:
        return [b"opus"]


async def _until(check: Callable[[], bool], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


@pytest.fixture(autouse=True)
def quiet_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def synth(*_: Any, **__: Any) -> tuple[bytes, int]:
        return b"\x00\x00" * 960, ws_session.SERVER_TTS_AUDIO_PARAMS.sample_rate

    monkeypatch.setattr(ws_session, "OpusEncoder", _FakeEncoder)
    monkeypatch.setattr(ws_session, "get_tts", lambda *a, **k: object())
    monkeypatch.setattr(ws_session, "synthesize_persona", synth)


async def _session(
    store: RegistryStore, device_id: str, config: dict[str, Any]
) -> tuple[_FakeWebSocket, asyncio.Task[None]]:
    await store.get_or_create_agent(device_id)
    ws = _FakeWebSocket(device_id)
    endpoint = ws_session.make_router(store, config).routes[0].endpoint
    return ws, asyncio.create_task(endpoint(ws))


async def _close(ws: _FakeWebSocket, session: asyncio.Task[None]) -> None:
    ws.inbox.put_nowait({"type": "websocket.disconnect"})
    await asyncio.wait_for(session, timeout=5)


async def test_an_idle_session_gets_keepalives(store: RegistryStore) -> None:
    session_state.mark_greeted("keepalive-idle")
    ws, session = await _session(store, "keepalive-idle", {"server": {"keepalive_seconds": 0.1}})

    await _until(lambda: len(ws.of_type("keepalive")) >= 3)
    hello = ws.of_type("hello")[0]
    assert ws.of_type("keepalive")[0] == {"type": "keepalive", "session_id": hello["session_id"]}

    await _close(ws, session)
    count = len(ws.of_type("keepalive"))
    await asyncio.sleep(0.3)
    assert len(ws.of_type("keepalive")) == count, "keepalive outlived the session"


async def test_keepalive_waits_for_the_hub_to_go_quiet() -> None:
    ws = _FakeWebSocket("keepalive-busy")
    clock = ws_session._SendClock(ws)  # type: ignore[arg-type]
    task = asyncio.create_task(ws_session._keep_alive(ws, clock, "s", 0.2))  # type: ignore[arg-type]

    # Talking every 50 ms: never 200 ms of silence, so never a keepalive.
    for _ in range(10):
        await ws.send_bytes(b"opus")
        await asyncio.sleep(0.05)
    assert ws.of_type("keepalive") == []

    await _until(lambda: len(ws.of_type("keepalive")) == 1, timeout=1)
    task.cancel()


async def test_zero_turns_keepalive_off(store: RegistryStore) -> None:
    session_state.mark_greeted("keepalive-off")
    ws, session = await _session(store, "keepalive-off", {"server": {"keepalive_seconds": 0}})
    await _until(lambda: bool(ws.of_type("hello")))
    await asyncio.sleep(0.2)
    assert ws.of_type("keepalive") == []
    await _close(ws, session)


async def test_a_reconnect_does_not_replay_the_greeting(store: RegistryStore) -> None:
    device = "keepalive-reconnect"
    session_state._greeted.discard(device)

    first, session = await _session(store, device, {})
    await _until(lambda: bool(first.of_type("tts")))
    await _close(first, session)
    assert [m["text"] for m in first.of_type("tts") if m["state"] == "sentence_start"] == [
        ws_session._GREETING
    ]

    again, session = await _session(store, device, {})
    await _until(lambda: bool(again.of_type("hello")))
    await asyncio.sleep(0.2)
    await _close(again, session)
    assert again.of_type("tts") == []
