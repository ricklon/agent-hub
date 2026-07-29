"""Tests that ports are a real trust boundary.

The dashboard can change personas, drive device tools, and read every
transcript. Operators open the device ports to the LAN, so the dashboard must
not answer there.
"""

from __future__ import annotations

from fastapi import FastAPI

from agent_hub.config import RegistryConfig, ServerConfig, Settings
from agent_hub.server import __main__ as server_main


def _routes(app: FastAPI) -> set[str]:
    return {getattr(route, "path", "") for route in app.router.routes}


def _configure(monkeypatch, tmp_path, **server_kwargs) -> Settings:
    settings = Settings(
        server=ServerConfig(**server_kwargs),
        registry=RegistryConfig(db_path=str(tmp_path / "registry.db")),
    )
    monkeypatch.setattr(server_main, "load_config", lambda: {})
    monkeypatch.setattr(server_main, "load_settings", lambda: settings)
    return settings


def test_distinct_ports_get_distinct_apps(monkeypatch, tmp_path) -> None:
    _configure(monkeypatch, tmp_path, ws_port=8000, http_port=8003, dashboard_port=8001)

    apps = server_main.build_apps()

    assert sorted(apps) == [8000, 8001, 8003]
    assert apps[8000] is not apps[8001]
    assert apps[8001] is not apps[8003]


def test_dashboard_absent_from_device_ports(monkeypatch, tmp_path) -> None:
    """The core guarantee: opening a device port must not expose the dashboard."""
    _configure(monkeypatch, tmp_path, ws_port=8000, http_port=8003, dashboard_port=8001)

    apps = server_main.build_apps()

    for device_port in (8000, 8003):
        paths = _routes(apps[device_port])
        assert not any(p.startswith("/dashboard") for p in paths), (
            f"dashboard routes leaked onto device port {device_port}: {sorted(paths)}"
        )
        assert "/" not in paths


def test_device_routes_absent_from_dashboard_port(monkeypatch, tmp_path) -> None:
    _configure(monkeypatch, tmp_path, ws_port=8000, http_port=8003, dashboard_port=8001)

    paths = _routes(server_main.build_apps()[8001])

    assert not any(p.startswith("/checkin") for p in paths)
    assert not any(p.startswith("/xiaozhi") for p in paths)
    assert "/" in paths  # the /dashboard/ redirect lives here only


def test_ws_and_image_share_the_device_port(monkeypatch, tmp_path) -> None:
    """image_url is derived from ws_url, so the image endpoint belongs on ws_port."""
    _configure(monkeypatch, tmp_path, ws_port=8000, http_port=8003, dashboard_port=8001)

    paths = _routes(server_main.build_apps()[8000])

    assert "/xiaozhi/v1/" in paths
    assert any("image" in p for p in paths)
    assert not any(p.startswith("/checkin") for p in paths)


def test_checkin_port_serves_checkin_and_ota(monkeypatch, tmp_path) -> None:
    _configure(monkeypatch, tmp_path, ws_port=8000, http_port=8003, dashboard_port=8001)

    paths = _routes(server_main.build_apps()[8003])

    assert "/checkin/" in paths
    assert "/xiaozhi/ota/" in paths
    assert "/xiaozhi/heartbeat/" in paths


def test_collapsed_ports_keep_every_route(monkeypatch, tmp_path) -> None:
    """Single-port setups are common in local dev and must not lose routes."""
    _configure(monkeypatch, tmp_path, ws_port=8000, http_port=8000, dashboard_port=8000)

    apps = server_main.build_apps()

    assert sorted(apps) == [8000]
    paths = _routes(apps[8000])
    assert "/checkin/" in paths
    assert "/xiaozhi/ota/" in paths
    assert "/xiaozhi/heartbeat/" in paths
    assert "/xiaozhi/v1/" in paths
    assert any(p.startswith("/dashboard") for p in paths)


def test_partially_collapsed_ports_merge_only_those_apps(monkeypatch, tmp_path) -> None:
    _configure(monkeypatch, tmp_path, ws_port=8000, http_port=8000, dashboard_port=8001)

    apps = server_main.build_apps()

    assert sorted(apps) == [8000, 8001]
    device_paths = _routes(apps[8000])
    assert "/checkin/" in device_paths
    assert "/xiaozhi/v1/" in device_paths
    assert not any(p.startswith("/dashboard") for p in device_paths)


def test_warns_when_dashboard_shares_a_device_port(monkeypatch, tmp_path) -> None:
    _configure(monkeypatch, tmp_path, ws_port=8000, http_port=8003, dashboard_port=8000)

    messages: list[str] = []
    monkeypatch.setattr(server_main.logger, "warning", lambda msg: messages.append(str(msg)))

    server_main.build_apps()

    assert any("shares port 8000" in m for m in messages)


def test_build_app_still_mounts_everything(monkeypatch, tmp_path) -> None:
    """build_app() is the embedding/testing escape hatch and keeps all routes."""
    _configure(monkeypatch, tmp_path)

    paths = _routes(server_main.build_app())

    assert "/checkin/" in paths
    assert "/xiaozhi/v1/" in paths
    assert any(p.startswith("/dashboard") for p in paths)


def test_allowed_hosts_installs_trusted_host_middleware(monkeypatch, tmp_path) -> None:
    _configure(
        monkeypatch,
        tmp_path,
        ws_port=8000,
        http_port=8003,
        dashboard_port=8001,
        allowed_hosts="hub.local, 192.168.1.5",
    )

    apps = server_main.build_apps()

    def _has_trusted_host(app: FastAPI) -> bool:
        return any("TrustedHost" in str(m.cls) for m in app.user_middleware)

    assert _has_trusted_host(apps[8001])
    # Devices connect by bare IP; a hostname allowlist on their ports would
    # reject check-in in a way that looks like a network fault.
    assert not _has_trusted_host(apps[8000])
    assert not _has_trusted_host(apps[8003])


def test_no_middleware_when_allowed_hosts_unset(monkeypatch, tmp_path) -> None:
    _configure(monkeypatch, tmp_path, ws_port=8000, http_port=8003, dashboard_port=8001)

    apps = server_main.build_apps()

    assert not any("TrustedHost" in str(m.cls) for m in apps[8001].user_middleware)


def test_parse_allowed_hosts_ignores_blanks() -> None:
    assert server_main.parse_allowed_hosts("a, ,b,") == ["a", "b"]
    assert server_main.parse_allowed_hosts("") == []


def test_warns_when_dashboard_exposed_without_password(monkeypatch, tmp_path) -> None:
    _configure(monkeypatch, tmp_path, host="0.0.0.0", dashboard_port=8001)

    messages: list[str] = []
    monkeypatch.setattr(server_main.logger, "warning", lambda msg: messages.append(str(msg)))

    server_main.build_apps()

    assert any("no password" in m for m in messages)


def test_no_exposure_warning_on_loopback(monkeypatch, tmp_path) -> None:
    _configure(monkeypatch, tmp_path, host="127.0.0.1", dashboard_port=8001)

    messages: list[str] = []
    monkeypatch.setattr(server_main.logger, "warning", lambda msg: messages.append(str(msg)))

    server_main.build_apps()

    assert not any("no password" in m for m in messages)


def test_no_exposure_warning_when_password_set(monkeypatch, tmp_path) -> None:
    _configure(monkeypatch, tmp_path, host="0.0.0.0", dashboard_password="hunter2")

    messages: list[str] = []
    monkeypatch.setattr(server_main.logger, "warning", lambda msg: messages.append(str(msg)))

    server_main.build_apps()

    assert not any("no password" in m for m in messages)
