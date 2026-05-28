"""Tests for wire protocol types in server/protocol.py.

Add a regression test for every protocol change. Use recorded fixtures
from a real device check-in where possible (see tests/fixtures/).
"""

from __future__ import annotations

import time

from agent_hub.server.protocol import (
    SERVER_TTS_AUDIO_PARAMS,
    CheckinRequest,
    CheckinResponse,
    ClientHello,
    ServerWelcome,
)


class TestCheckinRequest:
    def test_parse_minimal_headers(self):
        req = CheckinRequest.from_http(
            headers={"device-id": "AA:BB:CC:DD:EE:FF", "client-id": "client-1"},
            body={},
            client_host="192.168.1.10",
        )
        assert req.device_id == "AA:BB:CC:DD:EE:FF"
        assert req.client_id == "client-1"
        assert req.application_version == "0.0.0"
        assert req.board_type == "default"
        assert req.ip_address == "192.168.1.10"

    def test_parse_body_version_and_board(self):
        req = CheckinRequest.from_http(
            headers={"device-id": "AA:BB:CC:DD:EE:FF", "client-id": "c"},
            body={"application": {"version": "3.5.0"}, "board": {"type": "esp32s3"}},
            client_host="10.0.0.1",
        )
        assert req.application_version == "3.5.0"
        assert req.board_type == "esp32s3"

    def test_missing_device_id_raises(self):
        import pytest

        with pytest.raises(ValueError, match="device-id"):
            CheckinRequest.from_http(headers={"client-id": "c"}, body={}, client_host="")

    def test_missing_client_id_raises(self):
        import pytest

        with pytest.raises(ValueError, match="client-id"):
            CheckinRequest.from_http(
                headers={"device-id": "AA:BB:CC:DD:EE:FF"}, body={}, client_host=""
            )


class TestCheckinResponse:
    def test_to_json_shape(self):
        resp = CheckinResponse(websocket_url="ws://192.168.1.1:8000/xiaozhi/v1/")
        data = resp.to_json()
        assert "server_time" in data
        assert "timestamp" in data["server_time"]
        assert "timezone_offset" in data["server_time"]
        assert data["firmware"]["url"] == ""
        assert data["websocket"]["url"] == "ws://192.168.1.1:8000/xiaozhi/v1/"

    def test_timestamp_is_recent(self):
        before = int(time.time() * 1000)
        resp = CheckinResponse(websocket_url="ws://x/")
        after = int(time.time() * 1000)
        ts = resp.to_json()["server_time"]["timestamp"]
        assert before <= ts <= after

    def test_backward_compat_keys_present(self):
        """Regression: adding fields is OK; removing these keys breaks firmware."""
        resp = CheckinResponse(websocket_url="ws://x/")
        data = resp.to_json()
        assert set(data.keys()) >= {"server_time", "firmware", "websocket"}
        assert set(data["server_time"].keys()) >= {"timestamp", "timezone_offset"}
        assert set(data["firmware"].keys()) >= {"version", "url"}
        assert set(data["websocket"].keys()) >= {"url", "token"}


class TestClientHello:
    def test_parse_full_hello(self):
        raw = {
            "audio_params": {"format": "opus", "sample_rate": 16000},
            "features": {"mcp": True},
        }
        hello = ClientHello.from_json(raw)
        assert hello.audio_params.format == "opus"
        assert hello.audio_params.sample_rate == 16000
        assert hello.supports_mcp is True

    def test_parse_empty_hello(self):
        hello = ClientHello.from_json({})
        assert hello.audio_params.format == "opus"
        assert hello.supports_mcp is False

    def test_welcome_required_fields(self):
        """ServerWelcome sends server TTS params (not device params) and transport field."""
        welcome = ServerWelcome(session_id="abc123")
        data = welcome.to_json()
        assert data["type"] == "hello"
        assert data["transport"] == "websocket"
        assert data["session_id"] == "abc123"
        # Server always advertises its TTS downlink params, not the device's uplink params
        assert data["audio_params"]["sample_rate"] == SERVER_TTS_AUDIO_PARAMS.sample_rate
        assert data["audio_params"]["format"] == SERVER_TTS_AUDIO_PARAMS.format
