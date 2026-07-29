"""Security regression tests for dashboard exposure."""

from __future__ import annotations

import base64
import glob
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_hub.dashboard.app import make_router as make_dashboard_router
from agent_hub.registry.store import RegistryStore


def _basic_auth(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


@pytest.fixture()
def dashboard_config(tmp_path: Path) -> dict[str, object]:
    """Dashboard config with auth enabled and a temp image root."""
    return {
        "server": {
            "dashboard_username": "admin",
            "dashboard_password": "secret",
            "dashboard_image_root": str(tmp_path / "images"),
        }
    }


@pytest.fixture()
def dashboard_app(store: RegistryStore, dashboard_config: dict[str, object]) -> FastAPI:
    """FastAPI app with only dashboard routes mounted."""
    app = FastAPI()
    app.include_router(make_dashboard_router(store, dashboard_config))
    return app


@pytest.fixture()
def no_serial_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide serial ports from the reboot handler.

    /reboot falls back to writing !reboot over the first /dev/ttyACM* it finds.
    On a developer machine with a board attached that opens the real port and
    blocks, so any test that lets the handler run must stub discovery out.
    """
    monkeypatch.setattr(glob, "glob", lambda _pattern: [])


@pytest.fixture()
async def dashboard_client(dashboard_app: FastAPI) -> AsyncClient:
    """Authenticated dashboard test client."""
    async with AsyncClient(
        transport=ASGITransport(app=dashboard_app), base_url="http://test"
    ) as client:
        yield client


async def test_dashboard_requires_basic_auth(dashboard_client: AsyncClient) -> None:
    resp = await dashboard_client.get("/dashboard/")

    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Basic"


async def test_dashboard_accepts_valid_basic_auth(dashboard_client: AsyncClient) -> None:
    resp = await dashboard_client.get(
        "/dashboard/",
        headers={"Authorization": _basic_auth("admin", "secret")},
    )

    assert resp.status_code == 200
    assert "agent-hub" in resp.text


async def test_dashboard_image_rejects_paths_outside_image_root(
    dashboard_client: AsyncClient,
) -> None:
    resp = await dashboard_client.get(
        "/dashboard/image",
        params={"path": "/etc/passwd"},
        headers={"Authorization": _basic_auth("admin", "secret")},
    )

    assert resp.status_code == 404


async def test_dashboard_history_escapes_html_but_preserves_image_markers(
    dashboard_client: AsyncClient,
    dashboard_config: dict[str, object],
    store: RegistryStore,
) -> None:
    server_config = dashboard_config["server"]
    assert isinstance(server_config, dict)
    image_root = Path(str(server_config["dashboard_image_root"]))
    image_path = image_root / "AA-BB" / "capture.jpg"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"jpeg")

    await store.append_history(
        "AA:BB",
        "user",
        f'<script>alert("x")</script> [image:{image_path}]',
    )

    resp = await dashboard_client.get(
        "/dashboard/agents/AA:BB/history",
        headers={"Authorization": _basic_auth("admin", "secret")},
    )

    assert resp.status_code == 200
    assert "<script>" not in resp.text
    assert "&lt;script&gt;" in resp.text
    assert '<img src="/dashboard/image?path=' in resp.text


async def test_state_change_rejects_cross_origin_post(dashboard_client: AsyncClient) -> None:
    """A malicious page must not be able to drive device actions via CSRF."""
    resp = await dashboard_client.post(
        "/dashboard/agents/AA:BB/reboot",
        headers={
            "Authorization": _basic_auth("admin", "secret"),
            "Origin": "http://evil.example",
        },
    )

    assert resp.status_code == 403


async def test_state_change_allows_same_origin_post(
    dashboard_client: AsyncClient, no_serial_ports: None
) -> None:
    """The dashboard's own HTMX forms send a matching Origin."""
    resp = await dashboard_client.post(
        "/dashboard/agents/AA:BB/reboot",
        headers={
            "Authorization": _basic_auth("admin", "secret"),
            "Origin": "http://test",
        },
    )

    assert resp.status_code != 403


async def test_state_change_allows_missing_origin(
    dashboard_client: AsyncClient, no_serial_ports: None
) -> None:
    """Non-browser clients (curl, scripts/smoke.py) send no Origin and must still work.

    Browsers always send Origin on cross-origin POSTs, so absence is not a CSRF vector.
    """
    resp = await dashboard_client.post(
        "/dashboard/agents/AA:BB/reboot",
        headers={"Authorization": _basic_auth("admin", "secret")},
    )

    assert resp.status_code != 403


async def test_cross_origin_get_is_allowed(dashboard_client: AsyncClient) -> None:
    """The check guards state changes only; reads are unaffected."""
    resp = await dashboard_client.get(
        "/dashboard/",
        headers={
            "Authorization": _basic_auth("admin", "secret"),
            "Origin": "http://evil.example",
        },
    )

    assert resp.status_code == 200


async def test_origin_check_applies_without_dashboard_password(
    store: RegistryStore, tmp_path: Path
) -> None:
    """An unauthenticated LAN dashboard still must not accept drive-by POSTs."""
    app = FastAPI()
    app.include_router(
        make_dashboard_router(store, {"server": {"dashboard_image_root": str(tmp_path)}})
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/dashboard/agents/AA:BB/reboot",
            headers={"Origin": "http://evil.example"},
        )

    assert resp.status_code == 403


async def test_configured_extra_origin_is_allowed(
    store: RegistryStore, tmp_path: Path, no_serial_ports: None
) -> None:
    """A proxy that rewrites Host can be accommodated via config."""
    app = FastAPI()
    app.include_router(
        make_dashboard_router(
            store,
            {
                "server": {
                    "dashboard_image_root": str(tmp_path),
                    "dashboard_allowed_origins": "https://hub.example.com",
                }
            },
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/dashboard/agents/AA:BB/reboot",
            headers={"Origin": "https://hub.example.com"},
        )

    assert resp.status_code != 403


async def test_configured_allowlist_is_exhaustive(store: RegistryStore, tmp_path: Path) -> None:
    """Once an allowlist is set, the request's own Host is no longer trusted.

    This is the DNS-rebinding fix: an attacker who controls both Host and
    Origin (both naming their own domain) must still be rejected.
    """
    app = FastAPI()
    app.include_router(
        make_dashboard_router(
            store,
            {
                "server": {
                    "dashboard_image_root": str(tmp_path),
                    "dashboard_allowed_origins": "hub.local",
                }
            },
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://evil.example") as c:
        resp = await c.post(
            "/dashboard/agents/AA:BB/reboot",
            headers={"Origin": "http://evil.example"},
        )

    assert resp.status_code == 403


async def test_allowed_hosts_feeds_the_origin_allowlist(
    store: RegistryStore, tmp_path: Path, no_serial_ports: None
) -> None:
    """server.allowed_hosts is a valid Origin source, not just a Host filter."""
    app = FastAPI()
    app.include_router(
        make_dashboard_router(
            store,
            {"server": {"dashboard_image_root": str(tmp_path), "allowed_hosts": "hub.local"}},
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://hub.local") as c:
        ok = await c.post(
            "/dashboard/agents/AA:BB/reboot", headers={"Origin": "http://hub.local:8001"}
        )
        bad = await c.post(
            "/dashboard/agents/AA:BB/reboot", headers={"Origin": "http://evil.example"}
        )

    assert ok.status_code != 403  # bare host in allowlist matches host:port origin
    assert bad.status_code == 403


async def test_wildcard_allowlist_entry_does_not_disable_the_check(
    store: RegistryStore, tmp_path: Path
) -> None:
    """A '*' entry must not be treated as an allowed origin literal."""
    app = FastAPI()
    app.include_router(
        make_dashboard_router(
            store,
            {"server": {"dashboard_image_root": str(tmp_path), "allowed_hosts": "*,hub.local"}},
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://hub.local") as c:
        resp = await c.post(
            "/dashboard/agents/AA:BB/reboot", headers={"Origin": "http://evil.example"}
        )

    assert resp.status_code == 403


async def test_unconfigured_fallback_still_allows_same_origin(
    store: RegistryStore, tmp_path: Path, no_serial_ports: None
) -> None:
    """Zero-config classroom setups must keep working."""
    app = FastAPI()
    app.include_router(
        make_dashboard_router(store, {"server": {"dashboard_image_root": str(tmp_path)}})
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://192.168.1.5") as c:
        resp = await c.post(
            "/dashboard/agents/AA:BB/reboot", headers={"Origin": "http://192.168.1.5"}
        )

    assert resp.status_code != 403
