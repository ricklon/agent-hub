"""Smoke-check a running agent-hub server.

This script expects agent-hub to already be running via `just run`, Docker,
or another process. It probes the externally visible HTTP routes that demos
and devices depend on.
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class Probe:
    """One HTTP route probe."""

    name: str
    url: str
    expect_text: str


def _fetch(url: str, timeout: float) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "agent-hub-smoke"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(100_000).decode("utf-8", errors="replace")
        return response.status, body


def _check(probe: Probe, timeout: float) -> bool:
    try:
        status, body = _fetch(probe.url, timeout)
    except urllib.error.URLError as exc:
        print(f"FAIL {probe.name}: {probe.url} ({exc})")
        return False

    if status != 200:
        print(f"FAIL {probe.name}: {probe.url} returned HTTP {status}")
        return False

    if probe.expect_text not in body:
        print(f"FAIL {probe.name}: {probe.url} missing {probe.expect_text!r}")
        return False

    print(f"ok   {probe.name}: {probe.url}")
    return True


def main() -> int:
    """Run smoke probes and return a process exit code."""
    parser = argparse.ArgumentParser(description="Probe a running agent-hub server.")
    parser.add_argument("--host", default="127.0.0.1", help="Host or IP to probe.")
    parser.add_argument("--ws-port", type=int, default=8000, help="WebSocket/dashboard port.")
    parser.add_argument("--dashboard-port", type=int, default=8001, help="Alternate dashboard port.")
    parser.add_argument("--http-port", type=int, default=8003, help="Device check-in port.")
    parser.add_argument("--timeout", type=float, default=2.0, help="Per-request timeout seconds.")
    args = parser.parse_args()

    base_ws = f"http://{args.host}:{args.ws_port}"
    base_dash = f"http://{args.host}:{args.dashboard_port}"
    base_http = f"http://{args.host}:{args.http_port}"

    probes = [
        Probe("dashboard on ws port", f"{base_ws}/dashboard/", "agent-hub"),
        Probe("dashboard on dashboard port", f"{base_dash}/dashboard/", "agent-hub"),
        Probe("check-in endpoint", f"{base_http}/checkin/", "Check-in endpoint OK"),
        Probe("xiaozhi ota alias", f"{base_http}/xiaozhi/ota/", "Check-in endpoint OK"),
    ]

    passed = sum(1 for probe in probes if _check(probe, args.timeout))
    total = len(probes)
    if passed == total:
        print(f"smoke passed ({passed}/{total})")
        return 0
    print(f"smoke failed ({passed}/{total})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
