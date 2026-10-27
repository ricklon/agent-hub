"""A device ``abort`` stops the reply in progress and nothing else.

Coglet (and stock xiaozhi) firmware sends ``{"type":"abort"}`` when its button
interrupts a reply, and when quiet or calibration mode starts. After it, nothing
from the cancelled turn may reach the device: a late ``tts`` ``start`` would
switch a listening device back to speaking. The session stays open and the next
turn speaks normally.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

import pytest

from agent_hub.registry.store import RegistryStore
from agent_hub.server import session_state, ws_session
from agent_hub.server.mcp_client import MCPClient

_PACKETS_PER_SENTENCE = 30  # well past the pre-buffer, so the paced sender runs


class _FakeWebSocket:
    def __init__(self, device_id: str) -> None:
        self.headers = {"device-id": device_id}
        self.query_params: dict[str, str] = {}
        self.state = SimpleNamespace()
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent: list[Any] = []  # str for text frames, bytes for binary

    async def accept(self) -> None:
        pass

    async def close(self, code: int = 1000, reason: str = "") -> None:
        pass

    async def receive_text(self) -> str:
        return json.dumps(
            {
                "type": "hello",
                "version": 1,
                "features": {"mcp": False},
                "audio_params": {"format": "opus", "sample_rate": 16000, "frame_duration": 60},
            }
        )

    async def receive(self) -> dict[str, Any]:
        return await self.inbox.get()

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    def device_says(self, payload: dict[str, Any]) -> None:
        self.inbox.put_nowait({"type": "websocket.receive", "text": json.dumps(payload)})

    def device_sends_audio(self) -> None:
        self.inbox.put_nowait({"type": "websocket.receive", "bytes": b"mic"})

    def tts_states(self, since: int = 0) -> list[str]:
        states = []
        for frame in self.sent[since:]:
            if isinstance(frame, str):
                msg = json.loads(frame)
                if msg.get("type") == "tts":
                    states.append(msg["state"])
        return states


class _FakeVAD:
    def __init__(self, **_: Any) -> None:
        self._frames: list[bytes] = []

    def push(self, frame: bytes) -> bool:
        self._frames.append(frame)
        return False

    def take(self) -> list[bytes]:
        frames, self._frames = self._frames, []
        return frames


class _FakeEncoder:
    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    def encode(self, pcm: bytes) -> list[bytes]:
        return [b"opus"] * _PACKETS_PER_SENTENCE


async def _until(check: Callable[[], bool], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


@pytest.fixture()
def speech(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Voice turns that speak through the real streaming path.

    The first turn says one sentence, then waits on ``release`` as if the model
    were still thinking or a tool call were in flight, then would say more. Later
    turns say one sentence and finish.
    """
    ctl = SimpleNamespace(turns=0, release=asyncio.Event(), first_cancelled=asyncio.Event())

    async def synth(*_: Any, **__: Any) -> tuple[bytes, int]:
        return b"\x00\x00" * 960, ws_session.SERVER_TTS_AUDIO_PARAMS.sample_rate

    async def voice_turn(
        websocket: Any,
        frames: list[bytes],
        session_id: str,
        sample_rate: int,
        frame_duration: int,
        persona: Any,
        history: list[dict[str, str]],
        config: dict[str, Any],
        *_: Any,
        **__: Any,
    ) -> None:
        ctl.turns += 1
        first = ctl.turns == 1

        async def deltas() -> AsyncIterator[str]:
            yield "Hello there. " if first else "Second reply."
            if first:
                await ctl.release.wait()
                yield "This must never be spoken. "

        try:
            await ws_session._stream_reply_to_speech(
                websocket, deltas(), persona, config, session_id
            )
        except asyncio.CancelledError:
            if first:
                ctl.first_cancelled.set()
            raise

    monkeypatch.setattr(ws_session, "SileroVAD", _FakeVAD)
    monkeypatch.setattr(ws_session, "OpusEncoder", _FakeEncoder)
    monkeypatch.setattr(ws_session, "get_tts", lambda *a, **k: object())
    monkeypatch.setattr(ws_session, "synthesize_persona", synth)
    monkeypatch.setattr(ws_session, "_run_voice_turn", voice_turn)
    return ctl


async def _open_session(store: RegistryStore, device_id: str) -> tuple[_FakeWebSocket, Any]:
    await store.get_or_create_agent(device_id)
    session_state.mark_greeted(device_id)  # no greeting in the way
    ws = _FakeWebSocket(device_id)
    endpoint = ws_session.make_router(store, {}).routes[0].endpoint
    return ws, asyncio.create_task(endpoint(ws))


async def _close_session(ws: _FakeWebSocket, session: Any) -> None:
    ws.inbox.put_nowait({"type": "websocket.disconnect"})
    await asyncio.wait_for(session, timeout=5)


async def test_abort_stops_the_reply_and_nothing_from_it_follows(
    store: RegistryStore, speech: SimpleNamespace
) -> None:
    ws, session = await _open_session(store, "abort-mid-reply")

    ws.device_sends_audio()
    ws.device_says({"type": "listen", "state": "stop"})
    # Mid-reply: the first sentence has started and its audio is still pacing out.
    await _until(lambda: "sentence_start" in ws.tts_states() and b"opus" in ws.sent)

    ws.device_says({"type": "abort"})
    await asyncio.wait_for(speech.first_cancelled.wait(), timeout=5)
    after_abort = len(ws.sent)
    assert after_abort < 2 + _PACKETS_PER_SENTENCE + 1, "abort came too late to test"

    # Let the "model" carry on and give the paced sender time it would have used.
    speech.release.set()
    await asyncio.sleep(0.5)
    assert ws.sent[after_abort:] == []

    # The session is still open, and the next turn speaks normally.
    ws.device_sends_audio()
    ws.device_says({"type": "listen", "state": "stop"})
    await _until(lambda: "stop" in ws.tts_states(after_abort))
    assert ws.tts_states(after_abort) == ["start", "sentence_start", "stop"]

    await _close_session(ws, session)


async def test_abort_with_no_reply_in_progress_is_ignored(
    store: RegistryStore, speech: SimpleNamespace
) -> None:
    ws, session = await _open_session(store, "abort-while-idle")

    ws.device_says({"type": "abort"})
    speech.release.set()  # first turn would otherwise wait for it
    ws.device_sends_audio()
    ws.device_says({"type": "listen", "state": "stop"})
    await _until(lambda: "stop" in ws.tts_states())
    assert ws.tts_states() == ["start", "sentence_start", "sentence_start", "stop"]

    await _close_session(ws, session)


class _MCPWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


async def test_an_aborted_tool_call_drops_the_late_device_reply() -> None:
    ws = _MCPWebSocket()
    client = MCPClient(ws, "abort-tool-call")  # type: ignore[arg-type]
    call = asyncio.create_task(client.call_tool("self_coglet_gaze", {"horizontal": 10}))
    await _until(lambda: bool(ws.sent))
    call_id = ws.sent[-1]["payload"]["id"]

    call.cancel()  # what an abort does to the turn waiting on it
    with pytest.raises(asyncio.CancelledError):
        await call
    assert client._pending == {}

    # The device still ran the call; its reply arrives later and is dropped.
    reply = {"jsonrpc": "2.0", "id": call_id, "result": {"content": [{"type": "text"}]}}
    await client.handle_message(reply)

    # The client still works for the next turn.
    nxt = asyncio.create_task(client.call_tool("self_coglet_state", {}))
    await _until(lambda: len(ws.sent) == 2)
    next_id = ws.sent[-1]["payload"]["id"]
    ok = {"content": [{"type": "text", "text": "idle"}]}
    await client.handle_message({"jsonrpc": "2.0", "id": next_id, "result": ok})
    assert await nxt == "idle"


async def test_cancelling_a_started_slow_turn_cue_stops_it_at_once() -> None:
    spoke = asyncio.Event()
    finished = False

    async def cue() -> None:
        nonlocal finished
        spoke.set()
        await asyncio.sleep(10)
        finished = True

    slow = ws_session._DelayedTurnCue(0, cue)
    slow.start()
    await asyncio.wait_for(spoke.wait(), timeout=1)
    slow.cancel()
    await asyncio.wait_for(slow.close(), timeout=1)  # returns without waiting for the cue
    assert finished is False


async def test_session_close_still_cancels_an_abortable_turn(
    store: RegistryStore, speech: SimpleNamespace
) -> None:
    ws, session = await _open_session(store, "abort-on-close")
    ws.device_sends_audio()
    ws.device_says({"type": "listen", "state": "stop"})
    await _until(lambda: "sentence_start" in ws.tts_states())

    ws.inbox.put_nowait({"type": "websocket.disconnect"})
    await asyncio.wait_for(session, timeout=5)
    assert speech.first_cancelled.is_set()
