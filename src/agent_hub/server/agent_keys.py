"""Public keys that agents sign in with, and the message they sign.

An agent that pairs a key never holds a secret the hub also holds: the hub
keeps the public key, and the agent proves itself by signing a fresh session
message. Keys travel in OpenSSH format (``ssh-ed25519 AAAA… comment``), so an
existing SSH key works and ``ssh-keygen`` shows the same fingerprint the
dashboard does.

Only Ed25519 is accepted for now. ``key_type`` leaves room for RSA (ESP32-S3
boards, Step 6 of the pairing plan) and P-256 without changing callers.
"""

from __future__ import annotations

import base64
import hashlib
import time
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_ssh_public_key,
)

KEY_TYPE_ED25519 = "ssh-ed25519"

# A signed session message is accepted for this long either side of the
# hub's clock, and its nonce is remembered for twice as long so it can't be
# replayed while it is still fresh.
SESSION_CLOCK_SKEW_S = 120

_SESSION_PREFIX = "agent-hub-session-v1"


class InvalidAgentKey(ValueError):
    """Raised when a public key can't be used to sign in."""


@dataclass(frozen=True)
class ParsedKey:
    """A public key normalised for storage."""

    key_type: str
    public_key: str  # OpenSSH format, without the comment
    fingerprint: str  # SHA256:<base64>, as ssh-keygen -l prints it


def parse_public_key(text: str) -> ParsedKey:
    """Parse an OpenSSH public key line.

    Args:
        text: For example ``ssh-ed25519 AAAAC3Nz… robot@pi``.

    Returns:
        The key type, the key without its comment, and its fingerprint.

    Raises:
        InvalidAgentKey: The text isn't an OpenSSH public key, or isn't Ed25519.
    """
    parts = text.strip().split()
    if len(parts) < 2:
        raise InvalidAgentKey("expected an OpenSSH public key such as 'ssh-ed25519 AAAA…'")
    if parts[0] != KEY_TYPE_ED25519:
        raise InvalidAgentKey(f"only {KEY_TYPE_ED25519} keys are accepted, not {parts[0]!r}")
    try:
        key = load_ssh_public_key(f"{parts[0]} {parts[1]}".encode())
    except (ValueError, TypeError) as exc:
        raise InvalidAgentKey(f"not a valid {KEY_TYPE_ED25519} key") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise InvalidAgentKey(f"not a valid {KEY_TYPE_ED25519} key")
    openssh = key.public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode()
    blob = base64.b64decode(openssh.split()[1])
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return ParsedKey(KEY_TYPE_ED25519, openssh, f"SHA256:{digest}")


def session_message(agent_id: str, timestamp: int, nonce: str) -> bytes:
    """The exact bytes an agent signs to open a session.

    Args:
        agent_id: The agent signing in.
        timestamp: Unix seconds on the agent's clock.
        nonce: A random string used once.

    Returns:
        ``agent-hub-session-v1\\n<agent_id>\\n<timestamp>\\n<nonce>`` as UTF-8.
    """
    return f"{_SESSION_PREFIX}\n{agent_id}\n{timestamp}\n{nonce}".encode()


def verify_signature(public_key: str, message: bytes, signature_b64: str) -> bool:
    """Check a base64 signature over message against a stored public key.

    Args:
        public_key: OpenSSH public key as stored in ``agent_keys``.
        message: The signed bytes.
        signature_b64: Standard base64 of the raw signature.

    Returns:
        True only for a valid signature by that key.
    """
    try:
        key = load_ssh_public_key(public_key.encode())
        signature = base64.b64decode(signature_b64, validate=True)
    except (ValueError, TypeError):
        return False
    if not isinstance(key, Ed25519PublicKey):
        return False
    try:
        key.verify(signature, message)
    except InvalidSignature:
        return False
    return True


class NonceCache:
    """Remembers recently used session nonces so a signed message works once."""

    def __init__(self, ttl_s: int = 2 * SESSION_CLOCK_SKEW_S) -> None:
        """Create an empty cache.

        Args:
            ttl_s: How long a nonce is remembered.
        """
        self._ttl_s = ttl_s
        self._seen: dict[str, float] = {}

    def use(self, agent_id: str, nonce: str) -> bool:
        """Record a nonce. Returns False when it was already used."""
        now = time.monotonic()
        self._seen = {k: t for k, t in self._seen.items() if now - t < self._ttl_s}
        key = f"{agent_id}\n{nonce}"
        if key in self._seen:
            return False
        self._seen[key] = now
        return True
