"""In-memory per-device runtime state: discovered MCP tools, turn latency,
and active WebSocket session handles.

Intentionally simple — lives only for the server process lifetime.
The dashboard reads this alongside the DB to show live metrics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class TurnLatency:
    asr_ms: int = 0
    llm_ms: int = 0
    tts_ms: int = 0

    @property
    def total_ms(self) -> int:
        return self.asr_ms + self.llm_ms + self.tts_ms


@dataclass
class DeviceState:
    mcp_tools: list[str] = field(default_factory=list)
    last: TurnLatency = field(default_factory=TurnLatency)
    avg: TurnLatency = field(default_factory=TurnLatency)
    turns: int = 0


_state: dict[str, DeviceState] = {}

# Active WebSocket sessions: device_id → (speak_fn, send_json_fn)
_sessions: dict[str, tuple[
    Callable[[str], Awaitable[None]],
    Callable[[dict], Awaitable[None]],
]] = {}

# Active MCP clients: device_id → MCPClient (any to avoid circular import)
_mcp_clients: dict[str, Any] = {}


def register_session(
    device_id: str,
    speak: Callable[[str], Awaitable[None]],
    send_json: Callable[[dict], Awaitable[None]],
) -> None:
    _sessions[device_id] = (speak, send_json)


def unregister_session(device_id: str) -> None:
    _sessions.pop(device_id, None)
    _mcp_clients.pop(device_id, None)


def register_mcp_client(device_id: str, client: Any) -> None:
    _mcp_clients[device_id] = client


def get_mcp_client(device_id: str) -> Any | None:
    return _mcp_clients.get(device_id)


def get_speak(device_id: str) -> Callable[[str], Awaitable[None]] | None:
    entry = _sessions.get(device_id)
    return entry[0] if entry else None


def get_send_json(device_id: str) -> Callable[[dict], Awaitable[None]] | None:
    entry = _sessions.get(device_id)
    return entry[1] if entry else None


def is_connected(device_id: str) -> bool:
    return device_id in _sessions


def _get(device_id: str) -> DeviceState:
    if device_id not in _state:
        _state[device_id] = DeviceState()
    return _state[device_id]


def set_tools(device_id: str, tools: list[str]) -> None:
    _get(device_id).mcp_tools = tools


def record_turn(device_id: str, asr_ms: int, llm_ms: int, tts_ms: int) -> None:
    s = _get(device_id)
    s.last = TurnLatency(asr_ms, llm_ms, tts_ms)
    s.turns += 1
    if s.turns == 1:
        s.avg = TurnLatency(asr_ms, llm_ms, tts_ms)
    else:
        α = 0.3
        s.avg = TurnLatency(
            asr_ms=int(α * asr_ms + (1 - α) * s.avg.asr_ms),
            llm_ms=int(α * llm_ms + (1 - α) * s.avg.llm_ms),
            tts_ms=int(α * tts_ms + (1 - α) * s.avg.tts_ms),
        )


def get_state(device_id: str) -> DeviceState:
    return _get(device_id)


def all_devices() -> dict[str, DeviceState]:
    return _state


# ── Per-device greeting tracker (persists across reconnects) ─────────────────

_greeted: set[str] = set()


def has_greeted(device_id: str) -> bool:
    return device_id in _greeted


def mark_greeted(device_id: str) -> None:
    _greeted.add(device_id)
