"""A voice turn whose model call fails says so instead of going silent."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import openai
import pytest

from agent_hub.registry.models import Persona
from agent_hub.server import session_state, ws_session


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)


class _RemovedModel:
    async def stream_with_tools(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
        request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        raise openai.NotFoundError(
            "gone",
            response=httpx.Response(404, request=request),
            body={"message": "This model is unavailable for free."},
        )
        yield ""


async def test_a_failed_model_call_is_spoken_and_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    spoken: list[str] = []

    async def fake_speak(websocket: Any, text: str, *args: Any, **kwargs: Any) -> None:
        spoken.append(text)

    monkeypatch.setattr(ws_session, "get_llm", lambda *a, **k: _RemovedModel())
    monkeypatch.setattr(ws_session, "_speak", fake_speak)
    persona = Persona(
        name="p",
        llm_provider="openai",
        llm_model="minimax/minimax-m3:free",
        tts_provider="edge",
        asr_provider="moonshine",
        system_prompt="",
        memory_window=20,
    )
    history: list[dict[str, str]] = []

    result = await ws_session._run_llm_turn(
        _FakeSocket(),  # type: ignore[arg-type]
        "can you hear me?",
        "session-1",
        persona,
        history,
        {},
        None,
        "dev-voice",
        False,
    )

    assert result == (0, 0, 0, "")
    assert spoken == [ws_session._DEFAULT_MODEL_ERROR_NOTICE]
    # The unanswered utterance is not left dangling in the model's context.
    assert history == []
    error = session_state.get_llm_error("dev-voice")
    assert error is not None
    assert error["model"] == "minimax/minimax-m3:free"
    assert "removed or renamed" in error["error"]
