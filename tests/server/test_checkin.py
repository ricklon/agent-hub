"""Integration tests for the check-in endpoint.

Uses the FastAPI test client from conftest.py.
"""

from __future__ import annotations

import pytest


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


class TestCheckinOptions:
    async def test_options_returns_cors_headers(self, client):
        resp = await client.options("/xiaozhi/ota/")
        assert resp.status_code == 200
        assert "access-control-allow-origin" in resp.headers
