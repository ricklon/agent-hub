#!/usr/bin/env python3
"""Expose one hub-connected device's MCP tools to an MCP client over stdio.

A xiaozhi device is an MCP server, but its MCP messages ride inside the
WebSocket voice session and agent-hub is the only client that can reach them.
This proxy re-exposes that server to a local MCP client such as Claude Code:
``tools/list`` comes from the hub's live copy of the device's tool list, and
``tools/call`` goes through the hub to the device over its existing session.

Standard library only, so it runs under any ``python3``:

    claude mcp add coglet-c3 -- python3 scripts/device_mcp_proxy.py \\
        --device 9c:9e:6e:f7:16:0c

By default only tools that cannot move anything are exposed: status reads,
state, stop and release. Pass ``--tools a,b`` for an explicit list or
``--all-tools`` to expose everything the device offers. The allowlist is
enforced on calls as well as on the listing.

If the dashboard has a password, set AGENT_HUB_SERVER_DASHBOARD_USERNAME
(default ``admin``) and AGENT_HUB_SERVER_DASHBOARD_PASSWORD.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

PROTOCOL_VERSION = "2024-11-05"

# Name suffixes of tools that read state or stop motion, never start it.
SAFE_SUFFIXES = ("get_device_status", "_state", "_stop", "_release")


def log(message: str) -> None:
    """Write to stderr: stdout belongs to the MCP protocol."""
    print(f"device_mcp_proxy: {message}", file=sys.stderr, flush=True)


class Hub:
    """The few dashboard endpoints the proxy needs."""

    def __init__(self, base: str, device: str, timeout: float) -> None:
        self.base = base.rstrip("/")
        self.device = urllib.parse.quote(device, safe="")
        self.timeout = timeout
        self.headers = {"Content-Type": "application/json"}
        password = os.environ.get("AGENT_HUB_SERVER_DASHBOARD_PASSWORD", "")
        if password:
            user = os.environ.get("AGENT_HUB_SERVER_DASHBOARD_USERNAME", "admin")
            token = base64.b64encode(f"{user}:{password}".encode()).decode()
            self.headers["Authorization"] = f"Basic {token}"

    def _request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base}/dashboard/agents/{self.device}/{path}",
            data=data,
            headers=self.headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"null")
            except ValueError:
                return exc.code, {"error": exc.reason}

    def status(self) -> dict[str, Any]:
        code, body = self._request("GET", "status.json")
        if code != 200 or not isinstance(body, dict):
            raise RuntimeError(f"hub status returned HTTP {code}: {body}")
        return body

    def call(self, tool: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        _code, body = self._request(
            "POST", "call_tool.json", {"tool": tool, "arguments": arguments}
        )
        if isinstance(body, dict) and body.get("ok"):
            return True, str(body.get("result", ""))
        error = body.get("error") or body.get("detail") if isinstance(body, dict) else body
        return False, str(error)


class Proxy:
    def __init__(self, hub: Hub, allow: set[str] | None) -> None:
        self.hub = hub
        self.allow = allow  # None = expose everything

    def allowed(self, name: str) -> bool:
        if self.allow is None:
            return True
        if self.allow:
            return name in self.allow
        return name.endswith(SAFE_SUFFIXES)

    def list_tools(self) -> list[dict[str, Any]]:
        try:
            status = self.hub.status()
        except (OSError, RuntimeError) as exc:
            log(f"cannot reach the hub: {exc}")
            return []
        mcp = status.get("mcp") or {}
        if not mcp.get("ready"):
            log("device MCP is not ready; listing the tools the hub last saw")
        tools = []
        for tool in mcp.get("tools") or []:
            name = tool.get("name", "")
            if not name or not self.allowed(name):
                continue
            schema = tool.get("inputSchema") or {}
            if not schema:
                schema = {"type": "object", "properties": {}}
            tools.append(
                {"name": name, "description": tool.get("description", ""), "inputSchema": schema}
            )
        return tools

    def call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        if not self.allowed(name):
            return _tool_error(f"{name!r} is not exposed by this proxy")
        try:
            ok, result = self.hub.call(name, arguments)
        except OSError as exc:
            return _tool_error(f"cannot reach the hub: {exc}")
        if not ok:
            return _tool_error(result)
        if result.startswith("data:image"):
            header, _, data = result.partition(",")
            mime = header[len("data:") :].split(";")[0] or "image/jpeg"
            return {"content": [{"type": "image", "data": data, "mimeType": mime}]}
        return {"content": [{"type": "text", "text": result}]}

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        msg_id = message.get("id")
        if msg_id is None:
            return None  # notification, e.g. notifications/initialized
        if method == "initialize":
            result: Any = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agent-hub-device-proxy", "version": "0.1.0"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": self.list_tools()}
        elif method == "tools/call":
            result = self.call_tool(message.get("params") or {})
        else:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            }
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _tool_error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--device", required=True, help="device id, e.g. its MAC address")
    parser.add_argument(
        "--hub", default="http://127.0.0.1:8001", help="dashboard base URL (default %(default)s)"
    )
    parser.add_argument("--timeout", type=float, default=70.0, help="seconds per hub request")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--tools", help="comma-separated tool names to expose")
    group.add_argument("--all-tools", action="store_true", help="expose every device tool")
    args = parser.parse_args()

    if args.all_tools:
        allow: set[str] | None = None
    elif args.tools:
        allow = {t.strip() for t in args.tools.split(",") if t.strip()}
    else:
        allow = set()  # empty = the safe default set
    proxy = Proxy(Hub(args.hub, args.device, args.timeout), allow)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply: dict[str, Any] | None = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "parse error"},
            }
        else:
            reply = proxy.handle(message) if isinstance(message, dict) else None
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
