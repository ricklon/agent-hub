"""Resolve the real client IP from proxy headers, gated on proxy trust.

``X-Forwarded-For`` is set by whatever connects, so it is only consulted when
the request's socket peer is a configured trusted proxy
(``server.trusted_proxies``). LAN-first deployments leave that empty and always
use the socket peer, so a device on the LAN cannot spoof its recorded address.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable

_Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_trusted_proxies(raw: str) -> list[_Network]:
    """Parse a comma-separated list of proxy IPs or CIDR blocks.

    Bare addresses become single-host networks. Blank and unparseable entries
    are skipped rather than raising, so a typo disables trust instead of
    crashing check-in.
    """
    networks: list[_Network] = []
    for token in raw.split(","):
        candidate = token.strip()
        if not candidate:
            continue
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            continue
    return networks


def _in_networks(ip: str, networks: Iterable[_Network]) -> bool:
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return False
    return any(addr in net for net in networks)


def resolve_client_ip(
    *,
    socket_peer: str,
    forwarded_for: str,
    trusted_proxies: Iterable[_Network],
) -> str:
    """Return the best-known client IP for a request.

    When ``socket_peer`` is a trusted proxy, walk ``X-Forwarded-For`` from the
    right and return the first entry that is not itself a trusted proxy — the
    address the outermost trusted proxy accepted the connection from. In every
    other case (no trusted proxies configured, an untrusted peer, or a header
    listing only trusted proxies) return ``socket_peer`` unchanged.
    """
    networks = list(trusted_proxies)
    if not networks or not _in_networks(socket_peer, networks):
        return socket_peer

    hops = [hop.strip() for hop in forwarded_for.split(",") if hop.strip()]
    for hop in reversed(hops):
        if not _in_networks(hop, networks):
            return hop
    return socket_peer
