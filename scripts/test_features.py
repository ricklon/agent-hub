#!/usr/bin/env python3
"""Smoke-test agent-hub's available features against a live device.

Drives the dashboard ``/inject`` endpoint to exercise each feature end-to-end
(ASR is bypassed, but LLM + tool call + TTS-to-device all run) so you can
verify the server without speaking to the board. Also prints a static
capability report: loaded server skills and how the safe tool-policy
classifies the device's discovered MCP tools.

Run the server first (`uv run python -m agent_hub.server`) with a device
connected, then:

    uv run python scripts/test_features.py
    uv run python scripts/test_features.py --url http://192.168.5.6:8001
    uv run python scripts/test_features.py --device dc:da:0c:57:6f:94
    uv run python scripts/test_features.py --skip-camera   # camera is slow (~60s)

Exit code is non-zero if any non-skipped feature fails, so it doubles as a CI
/ pre-demo smoke check.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request

# Reply strings the inject endpoint returns when something went wrong.
_ERROR_SENTINELS = (
    "Device not connected",
    "Timed out waiting for reply",
    "Pipeline error",
    "Pipeline ran but produced no reply",
    "Empty message",
)

# (label, utterance, needs_api_key)
_BATTERY = [
    ("time skill (server)", "What time is it and what day is it?", False),
    ("weather skill (server)", "What's the weather in Boston, Massachusetts?", True),
    ("web search skill (server)", "Search the web for recent ESP32-S3 news.", True),
    ("camera (device MCP)", "What do you see right now? Take a photo.", False),
]


def _http(url: str, data: bytes | None = None, timeout: float = 100.0) -> str:
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).strip()


def discover_device(base: str) -> str | None:
    """Return the first device id found on the agents page, or None."""
    try:
        html = _http(f"{base}/dashboard/agents")
    except Exception as exc:
        print(f"  ! could not load {base}/dashboard/agents: {exc}")
        return None
    ids = re.findall(r"/dashboard/agents/([0-9a-fA-F:%]+?)[\"/]", html)
    seen: list[str] = []
    for raw in ids:
        dev = urllib.parse.unquote(raw)
        if dev and dev not in seen:
            seen.append(dev)
    return seen[0] if seen else None


def device_status(base: str, device: str) -> dict[str, object]:
    """Return dashboard JSON status for a device, or an empty dict."""
    dev_enc = urllib.parse.quote(device, safe="")
    try:
        raw = _http(f"{base}/dashboard/agents/{dev_enc}/status.json")
    except Exception:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def device_mcp_tool_names(status: dict[str, object]) -> list[str]:
    """Return discovered MCP tool names for one device."""
    mcp = status.get("mcp", {})
    tools = mcp.get("tools", []) if isinstance(mcp, dict) else []
    seen: list[str] = []
    for tool in tools:
        name = tool.get("name") if isinstance(tool, dict) else None
        if isinstance(name, str) and name not in seen:
            seen.append(name)
    return seen


def effective_tool_names(status: dict[str, object]) -> list[str]:
    """Return the persona-filtered MCP tool names for one device."""
    tools = status.get("effective_tool_allowlist", [])
    if not isinstance(tools, list):
        return []
    return [tool for tool in tools if isinstance(tool, str)]


def inject(base: str, dev_enc: str, text: str) -> tuple[bool, str, bool]:
    """POST an utterance. Returns (ok, reply_text, has_image)."""
    body = urllib.parse.urlencode({"text": text}).encode()
    try:
        html = _http(f"{base}/dashboard/agents/{dev_enc}/inject", data=body)
    except Exception as exc:
        return False, f"request failed: {exc}", False
    has_image = "<img" in html
    reply = _strip_tags(html).lstrip("▶").strip()
    ok = bool(reply) and not any(s in html for s in _ERROR_SENTINELS)
    return ok, reply, has_image


def static_report() -> None:
    """Print loaded skills + tool-policy classification (no server needed)."""
    try:
        import agent_hub.skills as skills
        from agent_hub.server import tool_policy
    except Exception as exc:  # pragma: no cover - import guard
        print(f"  ! could not import agent_hub for static report: {exc}")
        return
    names = [d["function"]["name"] for d in skills.get_definitions()]
    print(f"  server skills loaded ({len(names)}): {', '.join(names) or 'none'}")
    print("  tool-policy default classifies an empty/None allowlist as 'safe defaults':")
    sample = [
        "self_camera_take_photo",
        "self_get_device_status",
        "self_audio_speaker_set_volume",
        "self_system_reboot",
        "self_ota_firmware_update",
        "self_wifi_set_config",
    ]
    for t in sample:
        verdict = (
            "RISKY (needs explicit allowlist)"
            if tool_policy.is_risky_tool(t)
            else "safe (default-allowed)"
        )
        print(f"    - {t:<32} {verdict}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://localhost:8001", help="dashboard base URL")
    ap.add_argument("--device", default=None, help="device id (auto-detected if omitted)")
    ap.add_argument("--skip-camera", action="store_true", help="skip the slow camera turn")
    args = ap.parse_args()
    base = args.url.rstrip("/")

    print("=" * 64)
    print("agent-hub feature smoke test")
    print("=" * 64)

    print("\n[1] Static capabilities")
    static_report()

    print("\n[2] Live device")
    device = args.device or discover_device(base)
    if not device:
        print("  ! no device found. Is the server up and a board connected?")
        return 2
    dev_enc = urllib.parse.quote(device, safe="")
    status = device_status(base, device)
    mcp_tools = device_mcp_tool_names(status)
    effective_tools = effective_tool_names(status)
    print(f"  device: {device}")
    print(f"  discovered device MCP tools ({len(mcp_tools)}): {', '.join(mcp_tools) or 'none'}")
    print(
        f"  effective tool allowlist ({len(effective_tools)}): "
        f"{', '.join(effective_tools) or 'none'}"
    )

    print("\n[3] Feature battery (each drives a full pipeline turn)")
    print("    note: '[image attached]' now means the camera tool succeeded *this*")
    print("    turn; eyeball the scene description for correctness.")
    results: list[tuple[str, str]] = []
    for label, utterance, needs_key in _BATTERY:
        if args.skip_camera and "camera" in label:
            print(f"  - {label:<26} SKIPPED (--skip-camera)")
            continue
        print(f"  - {label:<26} asking: {utterance!r}")
        t0 = time.monotonic()
        ok, reply, has_image = inject(base, dev_enc, utterance)
        dt = time.monotonic() - t0
        status = "PASS" if ok else ("WARN" if needs_key else "FAIL")
        results.append((label, status))
        print(f"      {status} ({dt:.1f}s){' [image attached]' if has_image else ''}")
        print(f"      reply: {reply[:200]}")

    print("\n" + "=" * 64)
    fails = [label for label, s in results if s == "FAIL"]
    warns = [label for label, s in results if s == "WARN"]
    print(f"summary: {len(results)} run, {len(fails)} failed, {len(warns)} warned")
    if warns:
        print(f"  warned (may need an API key): {', '.join(warns)}")
    if fails:
        print(f"  FAILED: {', '.join(fails)}")
    print("=" * 64)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
