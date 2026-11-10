"""Robots pair an Ed25519 key with a code, then sign in by signature.

The flow under test: /agent/pair/start → a person types the code into Add
device → /agent/pair/poll says approved → /agent/session with a signature
returns the same token /agent/register would. Then the ways it must refuse:
bad signatures, replays, stale clocks, revoked keys, and the old token door.
"""

from __future__ import annotations

import base64
import secrets
import time
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from agent_hub.config import Settings
from agent_hub.dashboard.access_identity import OperatorIdentity
from agent_hub.dashboard.app import make_router as make_dashboard_router
from agent_hub.dashboard.authorization import DashboardAuthorization
from agent_hub.registry import pairing as pairing_module
from agent_hub.registry.models import AgentKind, OperatorRole
from agent_hub.registry.pairing import PairingRegistry
from agent_hub.registry.store import RegistryStore
from agent_hub.server import mcp_bridge
from agent_hub.server.agent_api import make_router as make_agent_api_router
from agent_hub.server.agent_keys import parse_public_key
from agent_hub.server.agent_pairing import make_router as make_pairing_router

_RICK = OperatorIdentity(email="rick@example.com", subject="sub-rick")
_ADA = OperatorIdentity(email="ada@example.com", subject="sub-ada")


class _FakeAuth(DashboardAuthorization):
    def __init__(
        self,
        store: RegistryStore,
        identity: OperatorIdentity | None,
        role: str = OperatorRole.OPERATOR.value,
    ) -> None:
        super().__init__(store, {})
        self._identity = identity
        self._role = role

    async def authenticate(self, request: Request) -> None:
        request.state.operator_identity = self._identity
        request.state.operator_role = self._role


def _app(
    store: RegistryStore,
    identity: OperatorIdentity | None = _RICK,
    role: str = OperatorRole.OPERATOR.value,
    enrollment_token: str = "",
) -> AsyncClient:
    settings = Settings.from_dict({"server": {"enrollment_token": enrollment_token}})
    app = FastAPI()
    app.include_router(make_agent_api_router(store, settings))
    app.include_router(make_pairing_router(store, settings))
    app.include_router(make_dashboard_router(store, {}, _FakeAuth(store, identity, role)))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _public(key: Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode()


def _signed(
    key: Ed25519PrivateKey,
    agent_id: str,
    *,
    timestamp: int | None = None,
    nonce: str | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ts = int(time.time()) if timestamp is None else timestamp
    n = nonce or secrets.token_urlsafe(12)
    message = f"agent-hub-session-v1\n{agent_id}\n{ts}\n{n}".encode()
    return {
        "agent_id": agent_id,
        "timestamp": ts,
        "nonce": n,
        "signature": base64.b64encode(key.sign(message)).decode(),
        "tools": tools or [],
    }


async def _pair(c: AsyncClient, key: Ed25519PrivateKey, **body: Any) -> dict[str, Any]:
    """Start pairing and approve it from the dashboard, as a person would."""
    started = await c.post("/agent/pair/start", json={"public_key": _public(key), **body})
    assert started.status_code == 200, started.text
    approved = await c.post("/dashboard/add-device", data={"code": started.json()["code"]})
    assert approved.status_code == 200, approved.text
    return dict(started.json())


async def test_the_whole_flow_pairs_signs_in_and_works(store: RegistryStore) -> None:
    key = Ed25519PrivateKey.generate()
    async with _app(store) as c:
        started = await c.post(
            "/agent/pair/start",
            json={"public_key": _public(key) + " robot@pi", "agent_id": "robot-01", "label": "arm"},
        )
        assert started.status_code == 200
        body = started.json()
        assert len(body["code"]) == 6 and body["code"].isdigit()
        assert body["fingerprint"] == parse_public_key(_public(key)).fingerprint

        waiting = await c.post("/agent/pair/poll", json={"attempt_id": body["attempt_id"]})
        assert waiting.status_code == 202

        page = await c.get("/dashboard/add-device")
        assert "robot-01" in page.text
        assert body["code"] not in page.text  # the code must come from the robot, not the page

        approved = await c.post("/dashboard/add-device", data={"code": body["code"]})
        assert "Paired arm" in approved.text

        done = await c.post("/agent/pair/poll", json={"attempt_id": body["attempt_id"]})
        assert done.status_code == 200
        assert done.json()["agent_id"] == "robot-01"

        tools = [{"name": "robot.wave", "description": "Wave.", "inputSchema": {}}]
        session = await c.post("/agent/session", json=_signed(key, "robot-01", tools=tools))
        assert session.status_code == 200, session.text
        token = session.json()["token"]

        beat = await c.post("/agent/heartbeat", json={"agent_id": "robot-01", "token": token})
        assert beat.status_code == 200

    agent = await store.get_agent("robot-01")
    assert agent is not None
    assert agent.kind == AgentKind.MCP.value
    assert agent.label == "arm"
    assert agent.owner_subject == _RICK.subject  # whoever typed the code owns it
    handle = mcp_bridge.get_page_agent("robot-01")
    assert handle is not None and "robot.wave" in handle.tools


async def test_a_wrong_signature_is_refused(store: RegistryStore) -> None:
    key, stranger = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    async with _app(store) as c:
        await _pair(c, key, agent_id="robot-02")
        resp = await c.post("/agent/session", json=_signed(stranger, "robot-02"))
    assert resp.status_code == 401


async def test_a_signed_message_works_once(store: RegistryStore) -> None:
    key = Ed25519PrivateKey.generate()
    async with _app(store) as c:
        await _pair(c, key, agent_id="robot-03")
        signed = _signed(key, "robot-03")
        assert (await c.post("/agent/session", json=signed)).status_code == 200
        replay = await c.post("/agent/session", json=signed)
    assert replay.status_code == 401
    assert "nonce" in replay.json()["message"]


async def test_a_stale_timestamp_is_refused(store: RegistryStore) -> None:
    key = Ed25519PrivateKey.generate()
    async with _app(store) as c:
        await _pair(c, key, agent_id="robot-04")
        old = _signed(key, "robot-04", timestamp=int(time.time()) - 3600)
        resp = await c.post("/agent/session", json=old)
    assert resp.status_code == 401


async def test_signing_in_before_approval_is_refused(store: RegistryStore) -> None:
    key = Ed25519PrivateKey.generate()
    async with _app(store) as c:
        await c.post("/agent/pair/start", json={"public_key": _public(key), "agent_id": "robot-05"})
        resp = await c.post("/agent/session", json=_signed(key, "robot-05"))
    assert resp.status_code == 401


async def test_revoking_signs_the_agent_out_and_stops_sign_in(store: RegistryStore) -> None:
    key = Ed25519PrivateKey.generate()
    async with _app(store) as c:
        await _pair(c, key, agent_id="robot-06")
        token = (await c.post("/agent/session", json=_signed(key, "robot-06"))).json()["token"]

        revoked = await c.post("/dashboard/agent-keys/robot-06/revoke")
        assert revoked.status_code == 303

        beat = await c.post("/agent/heartbeat", json={"agent_id": "robot-06", "token": token})
        assert beat.status_code == 401
        again = await c.post("/agent/session", json=_signed(key, "robot-06"))
        assert again.status_code == 401


async def test_someone_elses_agent_cannot_be_revoked_by_an_operator(store: RegistryStore) -> None:
    key = Ed25519PrivateKey.generate()
    async with _app(store, identity=_RICK) as c:
        await _pair(c, key, agent_id="robot-07")
    async with _app(store, identity=_ADA) as c:
        resp = await c.post("/dashboard/agent-keys/robot-07/revoke")
    assert resp.status_code == 400
    assert await PairingRegistry(store.session_factory).active_key("robot-07") is not None


async def test_the_old_token_door_is_closed_for_a_keyed_agent(store: RegistryStore) -> None:
    key = Ed25519PrivateKey.generate()
    async with _app(store) as c:
        await _pair(c, key, agent_id="robot-08")
        takeover = await c.post("/agent/register", json={"agent_id": "robot-08", "tools": []})
        other = await c.post("/agent/register", json={"agent_id": "robot-unkeyed", "tools": []})
    assert takeover.status_code == 409
    assert other.status_code == 200  # agents without keys are unaffected


async def test_pairing_a_new_key_replaces_the_old_one(store: RegistryStore) -> None:
    first, second = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    async with _app(store) as c:
        await _pair(c, first, agent_id="robot-09")
        await _pair(c, second, agent_id="robot-09")
        assert (await c.post("/agent/session", json=_signed(first, "robot-09"))).status_code == 401
        assert (await c.post("/agent/session", json=_signed(second, "robot-09"))).status_code == 200


async def test_wrong_codes_are_rate_limited(store: RegistryStore) -> None:
    key = Ed25519PrivateKey.generate()
    async with _app(store) as c:
        started = await c.post("/agent/pair/start", json={"public_key": _public(key)})
        code = started.json()["code"]
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            assert (await c.post("/dashboard/add-device", data={"code": wrong})).status_code == 400
        blocked = await c.post("/dashboard/add-device", data={"code": code})
    assert "Too many wrong codes" in blocked.text


async def test_only_ed25519_openssh_keys_are_accepted(store: RegistryStore) -> None:
    async with _app(store) as c:
        rsa = await c.post("/agent/pair/start", json={"public_key": "ssh-rsa AAAAB3NzaC1yc2E="})
        junk = await c.post("/agent/pair/start", json={"public_key": "ssh-ed25519 not-base64"})
    assert rsa.status_code == 400
    assert junk.status_code == 400


async def test_attempts_are_limited_per_address(store: RegistryStore, monkeypatch) -> None:
    monkeypatch.setattr(pairing_module, "MAX_LIVE_PER_IP", 2)
    async with _app(store) as c:
        statuses = [
            (
                await c.post(
                    "/agent/pair/start",
                    json={"public_key": _public(Ed25519PrivateKey.generate())},
                )
            ).status_code
            for _ in range(3)
        ]
    assert statuses == [200, 200, 429]


async def test_the_enrollment_token_filters_who_may_ask(store: RegistryStore) -> None:
    key = Ed25519PrivateKey.generate()
    async with _app(store, enrollment_token="class-secret") as c:
        without = await c.post("/agent/pair/start", json={"public_key": _public(key)})
        with_it = await c.post(
            "/agent/pair/start",
            json={"public_key": _public(key), "enrollment_token": "class-secret"},
        )
    assert without.status_code == 401
    assert with_it.status_code == 200
    # Having the token still doesn't pair anything: a person must type the code.
    agent = await store.get_agent(with_it.json()["agent_id"])
    assert agent is None


async def test_an_id_already_used_by_another_kind_is_refused(store: RegistryStore) -> None:
    await store.get_or_create_agent("aa:bb:cc:dd:ee:ff", kind=AgentKind.XIAOZHI)
    async with _app(store) as c:
        resp = await c.post(
            "/agent/pair/start",
            json={
                "public_key": _public(Ed25519PrivateKey.generate()),
                "agent_id": "aa:bb:cc:dd:ee:ff",
            },
        )
    assert resp.status_code == 409


async def test_an_expired_code_cannot_be_approved(store: RegistryStore, monkeypatch) -> None:
    from datetime import timedelta

    monkeypatch.setattr(pairing_module, "ATTEMPT_TTL", timedelta(seconds=-1))
    key = Ed25519PrivateKey.generate()
    async with _app(store) as c:
        started = (await c.post("/agent/pair/start", json={"public_key": _public(key)})).json()
        approve = await c.post("/dashboard/add-device", data={"code": started["code"]})
        poll = await c.post("/agent/pair/poll", json={"attempt_id": started["attempt_id"]})
    assert approve.status_code == 400
    assert poll.status_code == 410


async def test_checkin_does_not_mint_a_token_for_a_keyed_agent(store: RegistryStore) -> None:
    from agent_hub.server.checkin import make_router as make_checkin_router

    key = Ed25519PrivateKey.generate()
    async with _app(store) as c:
        await _pair(c, key, agent_id="robot-10")
    settings = Settings.from_dict({"server": {"enrollment_token": "enroll-secret"}})
    app = FastAPI()
    app.include_router(make_checkin_router(store, settings))
    headers = {
        "Device-Id": "robot-10",
        "Client-Id": "c",
        "X-Agent-Hub-Enrollment-Token": "enroll-secret",
    }
    other = {**headers, "Device-Id": "aa:bb:cc:dd:ee:01"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        keyed = await c.post("/checkin/", headers=headers, json={})
        unkeyed = await c.post("/checkin/", headers=other, json={})
    assert keyed.status_code == 200
    assert not keyed.json()["websocket"]["token"]
    assert unkeyed.json()["websocket"]["token"]  # devices without keys are unaffected
    assert (
        await store.validate_websocket_token("robot-10", keyed.json()["websocket"]["token"])
        is False
    )


async def test_a_code_cannot_pair_over_someone_elses_agent(store: RegistryStore) -> None:
    mine, theirs = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    async with _app(store, identity=_RICK) as c:
        await _pair(c, mine, agent_id="robot-11")
    pairing = PairingRegistry(store.session_factory)
    async with _app(store, identity=_ADA) as c:
        started = await c.post(
            "/agent/pair/start", json={"public_key": _public(theirs), "agent_id": "robot-11"}
        )
        page = await c.get("/dashboard/add-device")
        assert "replaces its current key" in page.text
        approved = await c.post("/dashboard/add-device", data={"code": started.json()["code"]})
    assert approved.status_code == 400
    key = await pairing.active_key("robot-11")
    assert key is not None and key.public_key == parse_public_key(_public(mine)).public_key
    agent = await store.get_agent("robot-11")
    assert agent is not None and agent.owner_subject == _RICK.subject


async def test_an_admin_may_re_pair_someone_elses_agent(store: RegistryStore) -> None:
    old, new = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    async with _app(store, identity=_RICK) as c:
        await _pair(c, old, agent_id="robot-12")
    async with _app(store, identity=_ADA, role=OperatorRole.ADMIN.value) as c:
        await _pair(c, new, agent_id="robot-12")
        assert (await c.post("/agent/session", json=_signed(new, "robot-12"))).status_code == 200
