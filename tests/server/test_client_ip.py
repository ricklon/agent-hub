"""Tests for trusted-proxy-gated client IP resolution."""

from __future__ import annotations

from agent_hub.server.client_ip import parse_trusted_proxies, resolve_client_ip


class TestParseTrustedProxies:
    def test_empty_string_is_no_proxies(self):
        assert parse_trusted_proxies("") == []
        assert parse_trusted_proxies("  ,  ") == []

    def test_bare_address_becomes_a_host_network(self):
        networks = parse_trusted_proxies("10.0.0.5")
        assert len(networks) == 1
        assert str(networks[0]) == "10.0.0.5/32"

    def test_cidr_and_list_with_whitespace(self):
        networks = parse_trusted_proxies("172.16.0.0/12, 10.0.0.1")
        assert [str(n) for n in networks] == ["172.16.0.0/12", "10.0.0.1/32"]

    def test_unparseable_entries_are_skipped_not_raised(self):
        networks = parse_trusted_proxies("not-an-ip, 192.168.1.0/24")
        assert [str(n) for n in networks] == ["192.168.1.0/24"]


class TestResolveClientIP:
    def test_no_trusted_proxies_returns_socket_peer(self):
        assert (
            resolve_client_ip(
                socket_peer="172.18.0.4",
                forwarded_for="203.0.113.7",
                trusted_proxies=[],
            )
            == "172.18.0.4"
        )

    def test_untrusted_peer_ignores_forwarded_for(self):
        # A device on the LAN cannot spoof its address by sending the header.
        trusted = parse_trusted_proxies("172.16.0.0/12")
        assert (
            resolve_client_ip(
                socket_peer="192.168.1.50",
                forwarded_for="203.0.113.7",
                trusted_proxies=trusted,
            )
            == "192.168.1.50"
        )

    def test_trusted_peer_uses_forwarded_for(self):
        trusted = parse_trusted_proxies("172.16.0.0/12")
        assert (
            resolve_client_ip(
                socket_peer="172.18.0.4",
                forwarded_for="203.0.113.7",
                trusted_proxies=trusted,
            )
            == "203.0.113.7"
        )

    def test_returns_rightmost_non_proxy_hop(self):
        # Device spoofs a leading entry; Caddy appends the address it saw.
        trusted = parse_trusted_proxies("172.16.0.0/12")
        assert (
            resolve_client_ip(
                socket_peer="172.18.0.4",
                forwarded_for="1.1.1.1, 203.0.113.7",
                trusted_proxies=trusted,
            )
            == "203.0.113.7"
        )

    def test_skips_trailing_trusted_hops(self):
        trusted = parse_trusted_proxies("172.16.0.0/12")
        assert (
            resolve_client_ip(
                socket_peer="172.18.0.4",
                forwarded_for="203.0.113.7, 172.18.0.9",
                trusted_proxies=trusted,
            )
            == "203.0.113.7"
        )

    def test_falls_back_to_peer_when_header_absent(self):
        trusted = parse_trusted_proxies("172.16.0.0/12")
        assert (
            resolve_client_ip(
                socket_peer="172.18.0.4",
                forwarded_for="",
                trusted_proxies=trusted,
            )
            == "172.18.0.4"
        )

    def test_falls_back_to_peer_when_every_hop_is_trusted(self):
        trusted = parse_trusted_proxies("172.16.0.0/12")
        assert (
            resolve_client_ip(
                socket_peer="172.18.0.4",
                forwarded_for="172.18.0.8, 172.18.0.9",
                trusted_proxies=trusted,
            )
            == "172.18.0.4"
        )
