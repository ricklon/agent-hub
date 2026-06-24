"""Security regression tests for dashboard exposure."""

from __future__ import annotations

import base64
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
