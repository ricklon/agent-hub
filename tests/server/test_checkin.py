"""Integration tests for the check-in endpoint.

Uses the FastAPI test client from conftest.py.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_hub.config import ServerConfig, Settings
from agent_hub.registry.store import RegistryStore
from agent_hub.server.checkin import make_router as make_checkin_router


@pytest.fixture()
def auth_settings() -> Settings:
    """Settings with check-in enrollment enabled."""
    return Settings(raw={"server": {"enrollment_token": "enroll-secret"}})


@pytest.fixture()
def auth_app(store: RegistryStore, auth_settings: Settings) -> FastAPI:
    """FastAPI test app with authenticated check-in mounted."""
    app = FastAPI()
    app.include_router(make_checkin_router(store, auth_settings))
    return app


@pytest.fixture()
async def auth_client(auth_app: FastAPI) -> AsyncClient:
    """Async HTTP client for authenticated check-in tests."""
    async with AsyncClient(transport=ASGITransport(app=auth_app), base_url="http://test") as c:
        yield c


class TestCheckinGet:
    async def test_get_returns_200(self, client):
        resp = await client.get("/checkin/")
        assert resp.status_code == 200

    async def test_alias_returns_200(self, client):
        resp = await client.get("/xiaozhi/ota/")
        assert resp.status_code == 200

    async def test_response_contains_websocket_text(self, client):
        resp = await client.get("/checkin/")
        assert "WebSocket" in resp.text


class TestCheckinPost:
    async def test_default_persona_prompt_requires_fresh_live_tools(self, store):
        persona = await store.get_persona_by_name("hub-default")

        assert persona is not None
        assert "always call the matching tool" in persona.system_prompt
        assert "Never reuse changing values" in persona.system_prompt

    async def test_post_minimal_headers(self, client):
        resp = await client.post(
            "/xiaozhi/ota/",
            headers={"device-id": "AA:BB:CC:DD:EE:FF", "client-id": "test-client"},
            json={"application": {"version": "3.5.0"}, "board": {"type": "esp32s3"}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "websocket" in data
        assert "url" in data["websocket"]
        assert "server_time" in data
        assert "firmware" in data

    async def test_post_registers_agent(self, client, store):
        await client.post(
            "/checkin/",
            headers={"device-id": "11:22:33:44:55:66", "client-id": "c"},
            json={},
        )
        agent = await store.get_agent("11:22:33:44:55:66")
        assert agent is not None
        assert agent.device_id == "11:22:33:44:55:66"

    async def test_post_stores_and_refreshes_board_name(self, client, store):
        headers = {"device-id": "11:22:33:44:55:77", "client-id": "c"}
        await client.post(
            "/checkin/",
            headers=headers,
            json={"board": {"type": "df-k10", "name": "UNIHIKER K10"}},
        )
        await client.post(
            "/checkin/",
            headers=headers,
            json={"board": {"type": "df-k10", "name": "Classroom K10"}},
        )

        agent = await store.get_agent("11:22:33:44:55:77")
        assert agent is not None
        assert agent.label == "Classroom K10"

    async def test_post_missing_device_id_returns_400(self, client):
        resp = await client.post(
            "/checkin/",
            headers={"client-id": "c"},
            json={},
        )
        assert resp.status_code == 400

    async def test_post_missing_client_id_returns_400(self, client):
        resp = await client.post(
            "/checkin/",
            headers={"device-id": "AA:BB:CC:DD:EE:FF"},
            json={},
        )
        assert resp.status_code == 400

    async def test_post_idempotent(self, client, store):
        headers = {"device-id": "AA:BB:CC:DD:EE:FF", "client-id": "c"}
        await client.post("/checkin/", headers=headers, json={})
        await client.post("/checkin/", headers=headers, json={})
        agent = await store.get_agent("AA:BB:CC:DD:EE:FF")
        assert agent is not None

    async def test_cors_headers_present(self, client):
        resp = await client.post(
            "/checkin/",
            headers={"device-id": "AA:BB:CC:DD:EE:FF", "client-id": "c"},
            json={},
        )
        assert resp.headers.get("access-control-allow-origin") == "*"

    async def test_timezone_name_overrides_fixed_offset(self, store):
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient

        from agent_hub.server.checkin import make_router

        settings = Settings(server=ServerConfig(timezone="America/New_York", timezone_offset=-8))
        app = FastAPI()
        app.include_router(make_router(store, settings))

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as test_client:
            resp = await test_client.post(
                "/checkin/",
                headers={"device-id": "AA:BB:CC:DD:EE:AA", "client-id": "c"},
                json={},
            )

        assert resp.status_code == 200
        expected = int(datetime.now(ZoneInfo("America/New_York")).utcoffset().total_seconds() // 60)
        assert resp.json()["server_time"]["timezone_offset"] == expected

    async def test_enrollment_required_when_configured(self, auth_client):
        resp = await auth_client.post(
            "/checkin/",
            headers={"device-id": "AA:BB:CC:DD:EE:FF", "client-id": "c"},
            json={},
        )
        assert resp.status_code == 401
        assert resp.json()["message"] == "invalid enrollment token"

    async def test_enrollment_header_issues_websocket_token(self, auth_client, store):
        resp = await auth_client.post(
            "/checkin/",
            headers={
                "device-id": "AA:BB:CC:DD:EE:FF",
                "client-id": "c",
                "x-agent-hub-enrollment-token": "enroll-secret",
            },
            json={},
        )
        assert resp.status_code == 200
        token = resp.json()["websocket"]["token"]
        assert token
        heartbeat = resp.json()["heartbeat"]
        assert heartbeat["url"].endswith("/xiaozhi/heartbeat/")
        assert heartbeat["token"] == token
        assert heartbeat["interval"] == 60
        assert heartbeat["enabled"] is True
        assert await store.validate_websocket_token("AA:BB:CC:DD:EE:FF", token)

    async def test_enrollment_query_issues_websocket_token(self, auth_client):
        resp = await auth_client.post(
            "/xiaozhi/ota/?enrollment_token=enroll-secret",
            headers={"device-id": "AA:BB:CC:DD:EE:FF", "client-id": "c"},
            json={},
        )
        assert resp.status_code == 200
        assert resp.json()["websocket"]["token"]


class TestCheckinOptions:
    async def test_options_returns_cors_headers(self, client):
        resp = await client.options("/xiaozhi/ota/")
        assert resp.status_code == 200
        assert "access-control-allow-origin" in resp.headers


class TestCheckinClientIP:
    """ip_address recording behind a reverse proxy (issue #44)."""

    @staticmethod
    async def _checkin(store, settings, *, peer, device_id, headers=None):
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient

        from agent_hub.server.checkin import make_router

        app = FastAPI()
        app.include_router(make_router(store, settings))
        transport = ASGITransport(app=app, client=(peer, 44444))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            await c.post(
                "/checkin/",
                headers={"device-id": device_id, "client-id": "c", **(headers or {})},
                json={},
            )
        return await store.get_agent(device_id)

    async def test_without_trusted_proxies_records_socket_peer(self, store):
        agent = await self._checkin(
            store,
            Settings(),
            peer="172.18.0.4",
            device_id="AA:BB:CC:DD:EE:01",
            headers={"x-forwarded-for": "203.0.113.7"},
        )
        assert agent is not None
        assert agent.ip_address == "172.18.0.4"

    async def test_trusted_proxy_records_forwarded_client_ip(self, store):
        settings = Settings(server=ServerConfig(trusted_proxies="172.16.0.0/12"))
        agent = await self._checkin(
            store,
            settings,
            peer="172.18.0.4",
            device_id="AA:BB:CC:DD:EE:02",
            headers={"x-forwarded-for": "203.0.113.7"},
        )
        assert agent is not None
        assert agent.ip_address == "203.0.113.7"

    async def test_untrusted_peer_cannot_spoof_via_header(self, store):
        settings = Settings(server=ServerConfig(trusted_proxies="172.16.0.0/12"))
        agent = await self._checkin(
            store,
            settings,
            peer="192.168.1.50",
            device_id="AA:BB:CC:DD:EE:03",
            headers={"x-forwarded-for": "203.0.113.7"},
        )
        assert agent is not None
        assert agent.ip_address == "192.168.1.50"
