"""Cloudflare Access operator identity tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import agent_hub.dashboard.app as dashboard_app_module
import agent_hub.dashboard.authorization as dashboard_auth_module
from agent_hub.config import Settings
from agent_hub.dashboard.access_identity import (
    AccessIdentityError,
    AccessIdentityVerifier,
    OperatorIdentity,
)
from agent_hub.registry.store import RegistryStore
from agent_hub.server.page_agent import make_router as make_page_agent_router

_TEAM_DOMAIN = "team.cloudflareaccess.com"
_AUDIENCE = "app-audience"
_KID = "test-key"


def _key_material() -> tuple[Any, dict[str, Any]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk["kid"] = _KID
    jwk["alg"] = "RS256"
    jwk["use"] = "sig"
    return private_key, jwk


def _assertion(private_key: Any, **overrides: Any) -> str:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "iss": f"https://{_TEAM_DOMAIN}",
        "aud": [_AUDIENCE],
        "sub": "operator-123",
        "email": "operator@example.com",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": _KID})


async def test_verifier_accepts_signed_access_identity_and_caches_keys() -> None:
    private_key, jwk = _key_material()
    requests = 0

    async def _certs(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"keys": [jwk]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_certs)) as client:
        verifier = AccessIdentityVerifier(
            _TEAM_DOMAIN,
            _AUDIENCE,
            http_client=client,
        )
        identity = await verifier.verify(_assertion(private_key))
        repeated = await verifier.verify(_assertion(private_key))

    assert identity == OperatorIdentity(email="operator@example.com", subject="operator-123")
    assert repeated == identity
    assert requests == 1


async def test_verifier_rejects_assertion_for_another_application() -> None:
    private_key, jwk = _key_material()

    async def _certs(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"keys": [jwk]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_certs)) as client:
        verifier = AccessIdentityVerifier(
            _TEAM_DOMAIN,
            _AUDIENCE,
            http_client=client,
        )
        with pytest.raises(AccessIdentityError):
            await verifier.verify(_assertion(private_key, aud=["another-app"]))


class _FakeVerifier:
    def __init__(self, team_domain: str, audience: str) -> None:
        assert team_domain == _TEAM_DOMAIN
        assert audience == _AUDIENCE

    async def verify(self, assertion: str) -> OperatorIdentity:
        identities = {
            "valid-assertion": OperatorIdentity(
                email="operator@example.com", subject="operator-123"
            ),
            "viewer-assertion": OperatorIdentity(email="viewer@example.com", subject="viewer-123"),
        }
        if assertion not in identities:
            raise AccessIdentityError("invalid")
        return identities[assertion]


def _access_config() -> dict[str, object]:
    return {
        "server": {
            "dashboard_access_team_domain": _TEAM_DOMAIN,
            "dashboard_access_audience": _AUDIENCE,
            "dashboard_admin_emails": "operator@example.com",
        }
    }


async def test_dashboard_shows_verified_operator_and_logout(
    store: RegistryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dashboard_auth_module, "AccessIdentityVerifier", _FakeVerifier)
    app = FastAPI()
    app.include_router(
        dashboard_app_module.make_router(
            store,
            _access_config(),
        )
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        denied = await client.get("/dashboard/")
        allowed = await client.get(
            "/dashboard/",
            headers={"Cf-Access-Jwt-Assertion": "valid-assertion"},
        )

    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert "operator@example.com" in allowed.text
    assert 'href="/cdn-cgi/access/logout"' in allowed.text
    assert "Admin" in allowed.text
    assert 'href="/dashboard/operators"' in allowed.text
    assert 'hx-headers=\'{"X-Requested-With":"XMLHttpRequest"}\'' in allowed.text


def test_dashboard_rejects_partial_access_configuration(store: RegistryStore) -> None:
    with pytest.raises(ValueError, match="must be configured together"):
        dashboard_app_module.make_router(
            store,
            {"server": {"dashboard_access_team_domain": _TEAM_DOMAIN}},
        )


def test_dashboard_requires_bootstrap_admin_for_access(store: RegistryStore) -> None:
    with pytest.raises(ValueError, match="dashboard_admin_emails"):
        dashboard_app_module.make_router(
            store,
            {
                "server": {
                    "dashboard_access_team_domain": _TEAM_DOMAIN,
                    "dashboard_access_audience": _AUDIENCE,
                }
            },
        )


async def test_new_access_identity_is_read_only_viewer(
    store: RegistryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dashboard_auth_module, "AccessIdentityVerifier", _FakeVerifier)
    app = FastAPI()
    app.include_router(dashboard_app_module.make_router(store, _access_config()))

    headers = {"Cf-Access-Jwt-Assertion": "viewer-assertion"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get("/dashboard/", headers=headers)
        mutation = await client.post(
            "/dashboard/models/select",
            headers=headers,
            data={"model_id": "example/model"},
        )
        operators = await client.get("/dashboard/operators", headers=headers)

    assert page.status_code == 200
    assert "Viewer" in page.text
    assert 'href="/dashboard/operators"' not in page.text
    assert 'href="/dashboard/page-agent"' not in page.text
    assert mutation.status_code == 403
    assert operators.status_code == 403


async def test_disabled_access_operator_is_rejected(
    store: RegistryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.registry.models import OperatorRole

    monkeypatch.setattr(dashboard_auth_module, "AccessIdentityVerifier", _FakeVerifier)
    await store.get_or_create_dashboard_operator("viewer-123", "viewer@example.com", set())
    assert await store.update_dashboard_operator("viewer-123", OperatorRole.VIEWER, enabled=False)
    app = FastAPI()
    app.include_router(dashboard_app_module.make_router(store, _access_config()))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/dashboard/",
            headers={"Cf-Access-Jwt-Assertion": "viewer-assertion"},
        )

    assert response.status_code == 403


async def test_admin_can_promote_viewer_to_operator(
    store: RegistryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dashboard_auth_module, "AccessIdentityVerifier", _FakeVerifier)
    app = FastAPI()
    app.include_router(dashboard_app_module.make_router(store, _access_config()))
    admin_headers = {"Cf-Access-Jwt-Assertion": "valid-assertion"}
    viewer_headers = {"Cf-Access-Jwt-Assertion": "viewer-assertion"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.get("/dashboard/", headers=viewer_headers)
        page = await client.get("/dashboard/operators", headers=admin_headers)
        promoted = await client.post(
            "/dashboard/operators/viewer-123",
            headers=admin_headers,
            data={"role": "operator", "enabled": "1"},
        )
        mutation = await client.post(
            "/dashboard/models/select",
            headers=viewer_headers,
            data={"model_id": "example/model"},
        )

    assert page.status_code == 200
    assert "viewer@example.com" in page.text
    assert promoted.status_code == 200
    assert mutation.status_code == 200


async def test_operator_management_route_protects_final_admin(
    store: RegistryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dashboard_auth_module, "AccessIdentityVerifier", _FakeVerifier)
    app = FastAPI()
    app.include_router(dashboard_app_module.make_router(store, _access_config()))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/dashboard/operators/operator-123",
            headers={"Cf-Access-Jwt-Assertion": "valid-assertion"},
            data={"role": "viewer", "enabled": "1"},
        )

    assert response.status_code == 409
    assert "final enabled admin" in response.text


async def test_viewer_cannot_register_browser_page_agent(
    store: RegistryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dashboard_auth_module, "AccessIdentityVerifier", _FakeVerifier)
    app = FastAPI()
    app.include_router(make_page_agent_router(store, Settings(), _access_config()))
    viewer_headers = {"Cf-Access-Jwt-Assertion": "viewer-assertion"}
    admin_headers = {"Cf-Access-Jwt-Assertion": "valid-assertion"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get("/dashboard/page-agent", headers=viewer_headers)
        register = await client.post(
            "/page-agent/register",
            headers=viewer_headers,
            json={"device_id": "viewer-page", "tools": []},
        )
        ask = await client.post(
            "/page-agent/ask",
            headers=viewer_headers,
            json={"device_id": "viewer-page", "token": "stolen", "text": "hello"},
        )
        admin_page = await client.get("/dashboard/page-agent", headers=admin_headers)

    assert page.status_code == 403
    assert register.status_code == 403
    assert ask.status_code == 403
    assert admin_page.status_code == 200


def _page_app(store: RegistryStore) -> FastAPI:
    # operator@example.com is the bootstrap admin; viewer-123 becomes an
    # operator on first sight, so there are two people who may register pages.
    config = _access_config()
    config["server"]["dashboard_default_role"] = "operator"  # type: ignore[index]
    app = FastAPI()
    app.include_router(make_page_agent_router(store, Settings(), config))
    return app


_ALICE = {"Cf-Access-Jwt-Assertion": "valid-assertion"}
_BOB = {"Cf-Access-Jwt-Assertion": "viewer-assertion"}


async def test_page_agent_is_owned_by_the_operator_who_registers_it(
    store: RegistryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.server import mcp_bridge

    monkeypatch.setattr(dashboard_auth_module, "AccessIdentityVerifier", _FakeVerifier)
    async with AsyncClient(
        transport=ASGITransport(app=_page_app(store)), base_url="http://test"
    ) as client:
        first = await client.post(
            "/page-agent/register", headers=_ALICE, json={"device_id": "page-a", "tools": []}
        )
        reload = await client.post(
            "/page-agent/register", headers=_ALICE, json={"device_id": "page-a", "tools": []}
        )

    assert first.json()["device_id"] == "page-a"
    assert reload.json()["device_id"] == "page-a"
    agent = await store.get_agent("page-a")
    assert agent is not None
    assert agent.owner_subject == "operator-123"
    assert agent.owner == "operator@example.com"
    mcp_bridge.unregister_page_agent("page-a")


async def test_operator_cannot_take_over_another_operators_page_agent(
    store: RegistryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.server import mcp_bridge

    monkeypatch.setattr(dashboard_auth_module, "AccessIdentityVerifier", _FakeVerifier)
    async with AsyncClient(
        transport=ASGITransport(app=_page_app(store)), base_url="http://test"
    ) as client:
        alice = await client.post(
            "/page-agent/register", headers=_ALICE, json={"device_id": "page-a", "tools": []}
        )
        bob = await client.post(
            "/page-agent/register", headers=_BOB, json={"device_id": "page-a", "tools": []}
        )

    alice_token = alice.json()["token"]
    bob_id = bob.json()["device_id"]
    assert bob.status_code == 200
    assert bob_id != "page-a"
    assert await store.validate_websocket_token("page-a", alice_token)
    handle = mcp_bridge.get_page_agent("page-a")
    assert handle is not None and handle.token == alice_token
    agent = await store.get_agent("page-a")
    assert agent is not None and agent.owner_subject == "operator-123"
    mcp_bridge.unregister_page_agent("page-a")
    mcp_bridge.unregister_page_agent(bob_id)


async def test_unclaimed_page_agent_is_adopted_only_when_not_live(
    store: RegistryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.registry.models import AgentKind
    from agent_hub.server import mcp_bridge

    monkeypatch.setattr(dashboard_auth_module, "AccessIdentityVerifier", _FakeVerifier)
    # Rows registered before ownership was recorded have no owner.
    await store.get_or_create_agent(device_id="page-idle", kind=AgentKind.PAGE)
    await store.get_or_create_agent(device_id="page-live", kind=AgentKind.PAGE)
    live_token = await store.issue_websocket_token("page-live")
    mcp_bridge.register_page_agent("page-live", live_token, []).connected = True

    async with AsyncClient(
        transport=ASGITransport(app=_page_app(store)), base_url="http://test"
    ) as client:
        idle = await client.post(
            "/page-agent/register", headers=_BOB, json={"device_id": "page-idle", "tools": []}
        )
        live = await client.post(
            "/page-agent/register", headers=_BOB, json={"device_id": "page-live", "tools": []}
        )

    assert idle.json()["device_id"] == "page-idle"
    adopted = await store.get_agent("page-idle")
    assert adopted is not None and adopted.owner_subject == "viewer-123"
    assert live.json()["device_id"] != "page-live"
    assert await store.validate_websocket_token("page-live", live_token)
    for device_id in ("page-idle", "page-live", live.json()["device_id"]):
        mcp_bridge.unregister_page_agent(device_id)


async def test_named_page_agent_is_scoped_to_its_owner(
    store: RegistryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.server import mcp_bridge

    monkeypatch.setattr(dashboard_auth_module, "AccessIdentityVerifier", _FakeVerifier)
    async with AsyncClient(
        transport=ASGITransport(app=_page_app(store)), base_url="http://test"
    ) as client:
        alice = await client.post(
            "/page-agent/register", headers=_ALICE, json={"name": "kitchen", "tools": []}
        )
        bob = await client.post(
            "/page-agent/register", headers=_BOB, json={"name": "kitchen", "tools": []}
        )

    alice_id = alice.json()["device_id"]
    bob_id = bob.json()["device_id"]
    assert alice.status_code == 200 and bob.status_code == 200
    assert alice_id != bob_id
    alice_agent = await store.get_agent(alice_id)
    bob_agent = await store.get_agent(bob_id)
    assert alice_agent is not None and alice_agent.owner_subject == "operator-123"
    assert alice_agent.label == "kitchen"
    assert bob_agent is not None and bob_agent.owner_subject == "viewer-123"
    mcp_bridge.unregister_page_agent(alice_id)
    mcp_bridge.unregister_page_agent(bob_id)


async def test_named_page_agent_refuses_a_row_owned_by_someone_else(
    store: RegistryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.registry.models import AgentKind
    from agent_hub.registry.page_identity import named_page_device_id

    monkeypatch.setattr(dashboard_auth_module, "AccessIdentityVerifier", _FakeVerifier)
    # The row Bob's "kitchen" maps to already belongs to Alice (a collision,
    # or an admin reassigning it). Bob must not get its token.
    bob_kitchen = named_page_device_id("viewer-123", "kitchen")
    await store.get_or_create_agent(
        device_id=bob_kitchen,
        kind=AgentKind.PAGE,
        owner="operator@example.com",
        owner_subject="operator-123",
    )
    alice_token = await store.issue_websocket_token(bob_kitchen)
    async with AsyncClient(
        transport=ASGITransport(app=_page_app(store)), base_url="http://test"
    ) as client:
        bob = await client.post(
            "/page-agent/register", headers=_BOB, json={"name": "kitchen", "tools": []}
        )

    assert bob.status_code == 409
    assert "someone else" in bob.json()["message"]
    assert await store.validate_websocket_token(bob_kitchen, alice_token)


async def test_mine_lists_only_the_callers_page_agents(
    store: RegistryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.server import mcp_bridge

    monkeypatch.setattr(dashboard_auth_module, "AccessIdentityVerifier", _FakeVerifier)
    async with AsyncClient(
        transport=ASGITransport(app=_page_app(store)), base_url="http://test"
    ) as client:
        alice = await client.post(
            "/page-agent/register", headers=_ALICE, json={"name": "kitchen", "tools": []}
        )
        bob = await client.post(
            "/page-agent/register", headers=_BOB, json={"name": "garage", "tools": []}
        )
        alice_mine = await client.get("/page-agent/mine", headers=_ALICE)
        bob_mine = await client.get("/page-agent/mine", headers=_BOB)

    assert [a["name"] for a in alice_mine.json()["agents"]] == ["kitchen"]
    assert [a["name"] for a in bob_mine.json()["agents"]] == ["garage"]
    mcp_bridge.unregister_page_agent(alice.json()["device_id"])
    mcp_bridge.unregister_page_agent(bob.json()["device_id"])
