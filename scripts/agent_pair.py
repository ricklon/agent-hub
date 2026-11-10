#!/usr/bin/env python3
"""Pair a robot or script with an agent-hub, then sign in with its key.

First run: makes an Ed25519 key, asks the hub to pair, and prints a code.
Type that code into Add device on the dashboard. After that, every run signs
in with the key; there is no secret to copy anywhere.

    python scripts/agent_pair.py --hub http://hub.local:8003 --name gripper

From robot code:

    from agent_pair import connect
    session = connect("http://hub.local:8003", "gripper", tools=[...])
    session["token"], session["mcp_event_url"], ...

Needs only the standard library and ``cryptography``. The key lives in
``~/.config/agent-hub/<name>_ed25519`` (or ``--key``, for an existing
OpenSSH Ed25519 key such as ``~/.ssh/id_ed25519``).
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_ssh_private_key,
)

CONFIG_DIR = Path(os.environ.get("AGENT_HUB_CONFIG_DIR", Path.home() / ".config" / "agent-hub"))
POLL_SECONDS = 3


class HubError(Exception):
    """The hub refused a request."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status


def _post(hub: str, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        hub.rstrip("/") + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except ValueError:
            payload = {}
        return exc.code, payload


def load_or_create_key(path: Path) -> Ed25519PrivateKey:
    """Load an OpenSSH Ed25519 private key, creating one (mode 600) if missing."""
    if path.exists():
        data = path.read_bytes()
        try:
            key = load_ssh_private_key(data, password=None)
        except (TypeError, ValueError):
            key = load_ssh_private_key(
                data, password=getpass.getpass(f"Passphrase for {path}: ").encode()
            )
        if not isinstance(key, Ed25519PrivateKey):
            raise SystemExit(f"{path} is not an Ed25519 key")
        return key
    key = Ed25519PrivateKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption()))
    path.chmod(0o600)
    path.with_suffix(".pub").write_text(public_key_line(key) + "\n")
    return key


def public_key_line(key: Ed25519PrivateKey) -> str:
    """The key's OpenSSH public form, ``ssh-ed25519 AAAA…``."""
    return key.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode()


def pair(
    hub: str,
    key: Ed25519PrivateKey,
    *,
    agent_id: str = "",
    label: str = "",
    kind: str = "mcp",
    persona: str = "",
    owner: str = "",
    enrollment_token: str = "",
) -> str:
    """Ask to pair, print the code, and wait until a person approves it.

    Returns:
        The agent id the hub paired.

    Raises:
        HubError: The hub refused, or the code expired.
    """
    body = {
        "public_key": public_key_line(key),
        "agent_id": agent_id,
        "label": label,
        "kind": kind,
        "persona": persona,
        "owner": owner,
        "enrollment_token": enrollment_token,
    }
    status, started = _post(hub, "/agent/pair/start", {k: v for k, v in body.items() if v})
    if status != 200:
        raise HubError(status, started.get("message", "pairing refused"))
    code = started["code"]
    print(f"\n  Pair code: {code[:3]} {code[3:]}\n", flush=True)
    print("  Open the hub dashboard → Add device, and type it in.", flush=True)
    print(f"  Key {started['fingerprint']}. Waiting…", flush=True)
    while True:
        time.sleep(POLL_SECONDS)
        status, polled = _post(hub, "/agent/pair/poll", {"attempt_id": started["attempt_id"]})
        if status == 200:
            print(f"  Paired as {polled['agent_id']}.", flush=True)
            return str(polled["agent_id"])
        if status != 202:
            raise HubError(status, polled.get("message", "pairing failed"))


def sign_in(
    hub: str, key: Ed25519PrivateKey, agent_id: str, tools: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Sign in with the key. Returns the token and bridge URLs.

    Raises:
        HubError: 401 when the key isn't paired (or was revoked).
    """
    timestamp = int(time.time())
    nonce = secrets.token_urlsafe(16)
    message = f"agent-hub-session-v1\n{agent_id}\n{timestamp}\n{nonce}".encode()
    status, session = _post(
        hub,
        "/agent/session",
        {
            "agent_id": agent_id,
            "timestamp": timestamp,
            "nonce": nonce,
            "signature": base64.b64encode(key.sign(message)).decode(),
            "tools": tools or [],
        },
    )
    if status != 200:
        raise HubError(status, session.get("message", "sign-in refused"))
    return session


def connect(
    hub: str,
    name: str,
    *,
    tools: list[dict[str, Any]] | None = None,
    key_path: Path | None = None,
    **pair_options: str,
) -> dict[str, Any]:
    """Sign in, pairing first if this name hasn't paired with this hub yet.

    Args:
        hub: The hub's device URL, e.g. ``http://hub.local:8003``.
        name: A short name for this robot; also its label when pairing.
        tools: MCP tool descriptions to register at sign-in.
        key_path: An existing key to use instead of ``~/.config/agent-hub/<name>_ed25519``.
        **pair_options: ``agent_id``, ``kind``, ``persona``, ``owner``,
            ``enrollment_token``, used only when pairing.

    Returns:
        The session: ``agent_id``, ``token``, and the bridge and heartbeat URLs.
    """
    key = load_or_create_key(key_path or CONFIG_DIR / f"{name}_ed25519")
    state_path = CONFIG_DIR / f"{name}.json"
    state: dict[str, str] = json.loads(state_path.read_text()) if state_path.exists() else {}
    if state.get("hub") == hub and state.get("agent_id"):
        try:
            return sign_in(hub, key, state["agent_id"], tools)
        except HubError as exc:
            if exc.status != 401:
                raise
            print("  This key isn't paired with the hub any more. Pairing again.", flush=True)
    pair_options.setdefault("label", name)
    agent_id = pair(hub, key, **pair_options)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"hub": hub, "agent_id": agent_id}))
    return sign_in(hub, key, agent_id, tools)


def main() -> None:
    """Command line: pair if needed, sign in once, and report."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--hub", required=True, help="device URL, e.g. http://hub.local:8003")
    parser.add_argument("--name", required=True, help="short name for this robot")
    parser.add_argument("--key", type=Path, help="existing OpenSSH Ed25519 private key")
    parser.add_argument("--kind", default="mcp", choices=["mcp", "voice", "ag2"])
    parser.add_argument("--persona", default="")
    parser.add_argument("--owner", default="")
    parser.add_argument(
        "--enrollment-token", default=os.environ.get("AGENT_HUB_ENROLLMENT_TOKEN", "")
    )
    args = parser.parse_args()
    try:
        session = connect(
            args.hub,
            args.name,
            key_path=args.key,
            kind=args.kind,
            persona=args.persona,
            owner=args.owner,
            enrollment_token=args.enrollment_token,
        )
    except HubError as exc:
        sys.exit(f"Hub said {exc}")
    print(f"Signed in as {session['agent_id']}. Token {session['token'][:6]}…")


if __name__ == "__main__":
    main()
