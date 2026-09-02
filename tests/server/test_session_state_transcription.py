"""In-memory transcription-session lifecycle."""

from __future__ import annotations

import re

from agent_hub.server import session_state


def _clean(device_id: str) -> None:
    session_state.end_transcription_session(device_id)


def test_start_returns_a_sortable_id_and_becomes_current() -> None:
    _clean("dev1")
    sid = session_state.start_transcription_session("dev1")
    assert re.match(r"\d{8}T\d{6}Z-[0-9a-f]{4}$", sid)
    assert session_state.current_transcription_session("dev1") == sid
    _clean("dev1")


def test_current_is_none_before_start_and_after_end() -> None:
    _clean("dev2")
    assert session_state.current_transcription_session("dev2") is None
    session_state.start_transcription_session("dev2")
    session_state.end_transcription_session("dev2")
    assert session_state.current_transcription_session("dev2") is None


def test_ensure_creates_once_then_reuses() -> None:
    _clean("dev3")
    a = session_state.ensure_transcription_session("dev3")
    b = session_state.ensure_transcription_session("dev3")
    assert a == b
    session_state.end_transcription_session("dev3")
    c = session_state.ensure_transcription_session("dev3")
    assert c != a
    _clean("dev3")


def test_sessions_are_per_device() -> None:
    _clean("devA")
    _clean("devB")
    a = session_state.start_transcription_session("devA")
    b = session_state.start_transcription_session("devB")
    assert a != b
    session_state.end_transcription_session("devA")
    assert session_state.current_transcription_session("devA") is None
    assert session_state.current_transcription_session("devB") == b
    _clean("devB")
