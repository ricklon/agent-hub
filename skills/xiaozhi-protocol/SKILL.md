---
name: xiaozhi-protocol
description: Use when writing or modifying code that reads or writes the Xiaozhi device wire protocol—check-in JSON, WebSocket hello messages, audio frames, tool calls, or MCP-over-WebSocket—including changes to server/protocol.py or server/checkin.py.
---

# Xiaozhi protocol

Preserve deployed-firmware compatibility while keeping Agent Hub a clean reimplementation.

## Required workflow

1. Read the relevant request construction in `78/xiaozhi-esp32` and response handling in `xinnan-tech/xiaozhi-esp32-server`; use them as protocol references only.
2. Inspect the existing parser, handler, and recorded fixtures before changing behavior.
3. Keep `/xiaozhi/ota/` as a permanent alias for `/checkin/`.
4. Add a regression test to `tests/server/test_protocol.py` for every wire-shape change and handler coverage to `tests/server/test_checkin.py`.
5. Run `just lint typecheck test`.

## Check-in contract

- Require `Device-Id` and `Client-Id` headers.
- Accept firmware metadata under `application` and hardware/network metadata under `board`.
- Current firmware supplies `board.type`, `board.name`, `board.manufacturer`, `board.ssid`, `board.rssi`, `board.channel`, `board.ip`, and `board.mac` when available.
- Treat `Device-Id` as stable identity. Treat board name, SSID, IP, and firmware as mutable metadata.
- Preserve response keys `server_time`, `firmware`, and `websocket`; additive fields are safe.
- Never introduce an activation gate. Enrollment authentication may protect a public endpoint, but a valid first check-in must auto-bind `hub-default`.
- Never log enrollment or per-device WebSocket tokens. Query strings can contain the enrollment token, so sanitize access logging when changing it.
- Accept authenticated liveness reports at `POST /xiaozhi/heartbeat/`. Require `Device-Id` and the per-device WebSocket token as a Bearer header; never accept that token in the URL.
- Heartbeat bodies use `{"health":"healthy"}` or `{"health":"degraded","fault":"..."}`. Keep activity out of the heartbeat because the voice pipeline owns it.
- Camera uploads with multipart `purpose=transcript` are capture-only snapshots. Save them as chronological `image` history turns containing an `[image:PATH]` marker, acknowledge immediately, and do not invoke vision inference. Uploads without that purpose retain image-explain behavior.

## WebSocket and audio constraints

- Preserve the firmware-required `hello` response with `transport: websocket` and `session_id`.
- Treat binary frames as Opus packets using the sample rate and frame duration negotiated in `audio_params`.
- Keep MCP messages inside the established voice WebSocket and retain JSON-RPC framing.

## References

- Firmware: <https://github.com/78/xiaozhi-esp32>
- Upstream server: <https://github.com/xinnan-tech/xiaozhi-esp32-server>
