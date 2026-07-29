"""Tests for the WebSocket enrollment-token gate.

The gate runs before ``websocket.accept()``, so these tests drive the route
endpoint directly with a fake WebSocket rather than a live client. An accepted
session immediately raises ``WebSocketDisconnect`` from the hello handshake so
the handler unwinds without touching any provider.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import WebSocketDisconnect

from agent_hub.registry.store import RegistryStore
from agent_hub.server.ws_session import make_router

_DEVICE = "AA:BB:CC:DD:EE:FF"
_ENROLLED_CONFIG: dict[str, Any] = {"server": {"enrollment_token": "enroll-secret"}}
_OPEN_CONFIG: dict[str, Any] = {}


class _FakeWebSocket:
    """Minimal WebSocket stand-in that disconnects as soon as it is accepted."""

    def __init__(self, headers: dict[str, str], query_params: dict[str, str]) -> None:
        self.headers = headers
        self.query_params = query_params
        self.accepted = False
        self.close_code: int | None = None
        self.close_reason: str | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_code = code
        self.close_reason = reason

    async def receive_text(self) -> str:
        raise WebSocketDisconnect(code=1000)


def _endpoint(store: RegistryStore, config: dict[str, Any]) -> Any:
    """Return the /xiaozhi/v1/ handler from a freshly built router."""
    router = make_router(store, config)
    return router.routes[0].endpoint


async def _connect(
    store: RegistryStore,
    config: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    query_params: dict[str, str] | None = None,
) -> _FakeWebSocket:
    ws = _FakeWebSocket(headers or {}, query_params or {})
    await _endpoint(store, config)(ws)
    return ws


@pytest.fixture()
async def enrolled_token(store: RegistryStore) -> str:
    """A registered device holding a current WebSocket token."""
    await store.get_or_create_agent(_DEVICE)
    return await store.issue_websocket_token(_DEVICE)


class TestDeviceId:
    async def test_missing_device_id_is_rejected(self, store):
        ws = await _connect(store, _OPEN_CONFIG)

        assert ws.accepted is False
        assert ws.close_code == 1008
        assert ws.close_reason == "missing device-id"

    async def test_device_id_may_come_from_query_params(self, store):
        ws = await _connect(store, _OPEN_CONFIG, query_params={"device-id": _DEVICE})

        assert ws.accepted is True


class TestEnrollmentEnabled:
    async def test_missing_token_is_rejected(self, store, enrolled_token):
        ws = await _connect(store, _ENROLLED_CONFIG, headers={"device-id": _DEVICE})

        assert ws.accepted is False
        assert ws.close_code == 1008
        assert ws.close_reason == "invalid token"

    async def test_wrong_token_is_rejected(self, store, enrolled_token):
        ws = await _connect(
            store,
            _ENROLLED_CONFIG,
            headers={"device-id": _DEVICE, "authorization": "Bearer not-the-token"},
        )

        assert ws.accepted is False
        assert ws.close_code == 1008

    async def test_bearer_header_token_is_accepted(self, store, enrolled_token):
        ws = await _connect(
            store,
            _ENROLLED_CONFIG,
            headers={"device-id": _DEVICE, "authorization": f"Bearer {enrolled_token}"},
        )

        assert ws.accepted is True
        assert ws.close_code is None

    async def test_bearer_scheme_is_case_insensitive(self, store, enrolled_token):
        ws = await _connect(
            store,
            _ENROLLED_CONFIG,
            headers={"device-id": _DEVICE, "authorization": f"bearer {enrolled_token}"},
        )

        assert ws.accepted is True

    async def test_query_param_token_is_accepted(self, store, enrolled_token):
        ws = await _connect(
            store,
            _ENROLLED_CONFIG,
            headers={"device-id": _DEVICE},
            query_params={"token": enrolled_token},
        )

        assert ws.accepted is True

    async def test_token_from_another_device_is_rejected(self, store, enrolled_token):
        await store.get_or_create_agent("11:22:33:44:55:66")

        ws = await _connect(
            store,
            _ENROLLED_CONFIG,
            headers={
                "device-id": "11:22:33:44:55:66",
                "authorization": f"Bearer {enrolled_token}",
            },
        )

        assert ws.accepted is False
        assert ws.close_code == 1008

    async def test_token_rotated_by_recheckin_invalidates_the_old_one(self, store, enrolled_token):
        # A device that reboots re-checks-in, which mints a fresh token. The
        # token it cached before the reboot must no longer open a session.
        await store.issue_websocket_token(_DEVICE)

        ws = await _connect(
            store,
            _ENROLLED_CONFIG,
            headers={"device-id": _DEVICE, "authorization": f"Bearer {enrolled_token}"},
        )

        assert ws.accepted is False
        assert ws.close_code == 1008

    async def test_unregistered_device_is_rejected(self, store):
        ws = await _connect(
            store,
            _ENROLLED_CONFIG,
            headers={"device-id": "99:99:99:99:99:99", "authorization": "Bearer anything"},
        )

        assert ws.accepted is False
        assert ws.close_code == 1008


class TestEnrollmentDisabled:
    """Field devices predate enrollment; with no token configured they must connect."""

    async def test_tokenless_device_is_accepted(self, store):
        ws = await _connect(store, _OPEN_CONFIG, headers={"device-id": _DEVICE})

        assert ws.accepted is True
        assert ws.close_code is None

    async def test_garbage_token_is_ignored(self, store):
        ws = await _connect(
            store,
            _OPEN_CONFIG,
            headers={"device-id": _DEVICE, "authorization": "Bearer stale-garbage"},
        )

        assert ws.accepted is True

    async def test_empty_enrollment_token_does_not_enable_the_gate(self, store):
        ws = await _connect(
            store,
            {"server": {"enrollment_token": ""}},
            headers={"device-id": _DEVICE},
        )

        assert ws.accepted is True
