"""Wire protocol types for xiaozhi device communication.

Check-in (POST /xiaozhi/ota/ or /checkin/):
  Request headers: device-id, client-id
  Request body (JSON): application.version, board.type
  Response JSON: server_time, firmware, websocket.{url, token}

WebSocket session (ws://.../xiaozhi/v1/):
  Connection headers: device-id, client-id, authorization (optional)
  Client → server:
    hello frame (JSON): {audio_params: {format, sample_rate}, features: {mcp}}
    audio frames (binary): Opus-encoded at sample_rate reported in hello
  Server → client:
    welcome frame (JSON): echoes audio_params back
    audio frames (binary): Opus-encoded TTS output at 24 kHz

  Audio format note: ESP32 firmware sends Opus packets at 16 kHz by default;
  TTS responses should be Opus-encoded at 24 kHz (server default).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CheckinRequest:
    """Parsed fields from a device check-in request."""

    device_id: str
    client_id: str
    application_version: str = "0.0.0"
    board_type: str = "default"
    ip_address: str = ""
    enrollment_token: str = ""

    @classmethod
    def from_http(
        cls,
        headers: dict[str, str],
        body: dict[str, Any],
        client_host: str,
        query_params: dict[str, str] | None = None,
    ) -> CheckinRequest:
        """Parse a check-in request from HTTP headers and body.

        Args:
            headers: Lowercased HTTP request headers.
            body: Parsed JSON body (may be empty dict).
            client_host: Remote IP address of the connecting device.
            query_params: URL query parameters from the check-in request.

        Returns:
            Populated CheckinRequest.

        Raises:
            ValueError: If device-id or client-id headers are missing.
        """
        device_id = headers.get("device-id", "").strip()
        if not device_id:
            raise ValueError("missing device-id header")
        client_id = headers.get("client-id", "").strip()
        if not client_id:
            raise ValueError("missing client-id header")

        app_version = (body.get("application") or {}).get("version", "0.0.0")
        board_type = (body.get("board") or {}).get("type", "default") or "default"
        query_params = query_params or {}
        auth_header = headers.get("authorization", "")
        bearer_token = auth_header.removeprefix("Bearer ").strip()
        enrollment_token = (
            headers.get("x-agent-hub-enrollment-token", "")
            or bearer_token
            or query_params.get("enrollment_token", "")
            or str((body.get("agent_hub") or {}).get("enrollment_token", ""))
            or str(body.get("enrollment_token", ""))
        ).strip()

        return cls(
            device_id=device_id,
            client_id=client_id,
            application_version=app_version or "0.0.0",
            board_type=board_type,
            ip_address=client_host,
            enrollment_token=enrollment_token,
        )


@dataclass
class CheckinResponse:
    """JSON body returned to a device on check-in."""

    websocket_url: str
    server_timestamp_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    timezone_offset_minutes: int = -480  # PST
    firmware_version: str = ""
    firmware_url: str = ""
    token: str = ""
    image_url: str = ""
    image_token: str = ""

    def to_json(self) -> dict[str, Any]:
        """Serialize to the upstream-compatible JSON wire format.

        Returns:
            Dict ready for json.dumps or FastAPI's JSONResponse.
        """
        d: dict[str, Any] = {
            "server_time": {
                "timestamp": self.server_timestamp_ms,
                "timezone_offset": self.timezone_offset_minutes,
            },
            "firmware": {
                "version": self.firmware_version,
                "url": self.firmware_url,
            },
            "websocket": {
                "url": self.websocket_url,
                "token": self.token,
            },
        }
        if self.image_url:
            d["image"] = {"url": self.image_url, "token": self.image_token}
        return d


@dataclass
class AudioParams:
    """Audio stream parameters for one direction of the WS audio channel.

    The device hello describes its uplink (mic→server): typically 16 kHz, 60 ms.
    The server hello describes its downlink (TTS→device): typically 24 kHz, 20 ms.
    """

    format: str = "opus"
    sample_rate: int = 16000
    channels: int = 1
    frame_duration: int = 60  # ms; 60 ms = 960 samples at 16 kHz (device default)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AudioParams:
        """Parse from the audio_params sub-object in a hello message.

        Args:
            data: The audio_params dict from the JSON message.

        Returns:
            AudioParams instance.
        """
        return cls(
            format=data.get("format", "opus"),
            sample_rate=int(data.get("sample_rate", 16000)),
            channels=int(data.get("channels", 1)),
            frame_duration=int(data.get("frame_duration", 60)),
        )

    def to_json(self) -> dict[str, Any]:
        """Serialize to JSON-serializable dict."""
        return {
            "format": self.format,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "frame_duration": self.frame_duration,
        }

    @property
    def frame_samples(self) -> int:
        """Number of PCM samples per Opus frame."""
        return self.sample_rate * self.frame_duration // 1000


# Audio params the server advertises for its TTS downlink.
# Must match AUDIO_OUTPUT_SAMPLE_RATE=16000 on the XIAO ESP32-S3 board.
SERVER_TTS_AUDIO_PARAMS = AudioParams(
    format="opus",
    sample_rate=16000,
    channels=1,
    frame_duration=60,  # 960 samples — matches device uplink frame size
)


@dataclass
class ClientHello:
    """First message sent by the device after a WebSocket connection opens."""

    audio_params: AudioParams = field(default_factory=AudioParams)
    features: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ClientHello:
        """Parse a client hello message.

        Args:
            data: Parsed JSON dict from the client.

        Returns:
            ClientHello instance.
        """
        return cls(
            audio_params=AudioParams.from_json(data.get("audio_params") or {}),
            features=data.get("features") or {},
        )

    @property
    def supports_mcp(self) -> bool:
        """True if the client advertised MCP tool support."""
        return bool(self.features.get("mcp"))

    @property
    def supports_emoji(self) -> bool:
        """True if the client can display emotion faces (features.emoji)."""
        return bool(self.features.get("emoji"))


@dataclass
class ServerWelcome:
    """Welcome frame sent by the server in response to a client hello.

    The device checks for ``transport == "websocket"`` before marking the
    audio channel as open; missing it causes a 10-second timeout and
    disconnect.  ``session_id`` is echoed back in all subsequent messages.
    """

    session_id: str
    audio_params: AudioParams = field(default_factory=lambda: SERVER_TTS_AUDIO_PARAMS)

    def to_json(self) -> dict[str, Any]:
        """Serialize to the firmware-compatible welcome wire format.

        Returns:
            Dict ready for json.dumps.
        """
        return {
            "type": "hello",
            "transport": "websocket",
            "session_id": self.session_id,
            "audio_params": self.audio_params.to_json(),
        }
