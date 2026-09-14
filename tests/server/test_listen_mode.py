"""Listen mode: voice/dashboard toggle that transcribes and only says okay."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_hub.dashboard.app import make_router
from agent_hub.providers.asr import Transcript
from agent_hub.registry.models import AgentKind
from agent_hub.registry.store import RegistryStore
from agent_hub.server import listen_mode
from agent_hub.server.ws_session import _run_voice_turn

DEVICE = "9c:9e:6e:f7:16:0c"


@pytest.fixture(autouse=True)
def _reset_listen_mode():
    listen_mode.set_listen_only(DEVICE, False)
    yield
    listen_mode.set_listen_only(DEVICE, False)


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        ("Robot, go into listen mode.", "listen"),
        ("robot go into listening mode", "listen"),
        ("Listen only, please.", "listen"),
        ("OK robot, interact again!", "interact"),
        ("Robot, exit listen mode.", "interact"),
        ("Stop listen mode", "interact"),
        ("listen mode off", "interact"),
        ("Interactive mode.", "interact"),
        ("Please listen to me and look left.", None),
        ("What's the weather like?", None),
    ],
)
def test_parse_command(transcript: str, expected: str | None) -> None:
    assert listen_mode.parse_command(transcript) == expected


async def _voice_turn(monkeypatch, text: str) -> tuple[list[dict], list[str], list[dict]]:
    """Run one voice turn with ``text`` as the ASR result; return (sent, spoken, logged)."""
    sent: list[dict] = []
    spoken: list[str] = []
    logged: list[dict] = []

    class _WebSocket:
        async def send_text(self, payload: str) -> None:
            sent.append(json.loads(payload))

    class _Decoder:
        def __init__(self, *_args) -> None:
            pass

        def decode(self, _frame: bytes) -> bytes:
            return b"\x00\x00"

    class _ASR:
        async def transcribe(self, _wav: bytes) -> Transcript:
            return Transcript(text=text, language="en")

    async def _no_llm(*_args, **_kwargs):
        raise AssertionError("listen mode ran an LLM turn")

    async def _speak(_ws, words, *_args, **_kwargs) -> None:
        spoken.append(words)

    monkeypatch.setattr("agent_hub.server.ws_session.OpusDecoder", _Decoder)
    monkeypatch.setattr("agent_hub.server.ws_session.get_asr", lambda *_args: _ASR())
    monkeypatch.setattr("agent_hub.server.ws_session._run_llm_turn", _no_llm)
    monkeypatch.setattr("agent_hub.server.ws_session._speak", _speak)
    monkeypatch.setattr(
        "agent_hub.server.ws_session.transcript_log.log_turn", lambda **kw: logged.append(kw)
    )

    await _run_voice_turn(
        _WebSocket(),  # type: ignore[arg-type]
        [b"opus"] * 20,
        "session-1",
        16000,
        60,
        SimpleNamespace(asr_provider="fake"),  # type: ignore[arg-type]
        [],
        {},
        None,
        DEVICE,
    )
    return sent, spoken, logged


async def test_voice_command_enters_listen_mode_and_confirms(monkeypatch) -> None:
    sent, spoken, _ = await _voice_turn(monkeypatch, "Robot, go into listen mode.")

    assert listen_mode.is_listen_only(DEVICE)
    assert spoken == [listen_mode.LISTEN_CONFIRMATION]
    assert sent[0]["type"] == "stt"


async def test_listen_mode_logs_speech_and_only_says_okay(monkeypatch) -> None:
    listen_mode.set_listen_only(DEVICE, True)

    sent, spoken, logged = await _voice_turn(monkeypatch, "Look to the left and wink at me.")

    assert spoken == [listen_mode.LISTEN_ACK]
    assert [m["type"] for m in sent] == ["stt"]
    assert len(logged) == 1
    assert logged[0]["text"] == "Look to the left and wink at me." and logged[0]["reply"] == ""


async def test_voice_command_leaves_listen_mode(monkeypatch) -> None:
    listen_mode.set_listen_only(DEVICE, True)

    _, spoken, _ = await _voice_turn(monkeypatch, "OK robot, interact again.")

    assert not listen_mode.is_listen_only(DEVICE)
    assert spoken == [listen_mode.INTERACT_CONFIRMATION]


async def test_dashboard_listen_toggle(store: RegistryStore) -> None:
    await store.get_or_create_agent(DEVICE, kind=AgentKind.XIAOZHI, label="coglet-c3")
    app = FastAPI()
    app.include_router(make_router(store, {}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        on = await c.post(f"/dashboard/agents/{DEVICE}/listen", data={"listen": "1"})
        assert listen_mode.is_listen_only(DEVICE)
        off = await c.post(f"/dashboard/agents/{DEVICE}/listen", data={"listen": "0"})
        missing = await c.post("/dashboard/agents/nope/listen", data={"listen": "1"})

    assert on.status_code == 200 and "interact again" in on.text
    assert off.status_code == 200 and "only says okay" in off.text
    assert not listen_mode.is_listen_only(DEVICE)
    assert missing.status_code == 404
