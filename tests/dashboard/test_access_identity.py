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
from agent_hub.dashboard.access_identity import (
    AccessIdentityError,
    AccessIdentityVerifier,
    OperatorIdentity,
)
from agent_hub.registry.store import RegistryStore

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
        if assertion != "valid-assertion":
            raise AccessIdentityError("invalid")
        return OperatorIdentity(email="operator@example.com", subject="operator-123")


async def test_dashboard_shows_verified_operator_and_logout(
    store: RegistryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dashboard_app_module, "AccessIdentityVerifier", _FakeVerifier)
    app = FastAPI()
    app.include_router(
        dashboard_app_module.make_router(
            store,
            {
                "server": {
                    "dashboard_access_team_domain": _TEAM_DOMAIN,
                    "dashboard_access_audience": _AUDIENCE,
                }
            },
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
    assert "Operator" in allowed.text
    assert 'hx-headers=\'{"X-Requested-With":"XMLHttpRequest"}\'' in allowed.text


def test_dashboard_rejects_partial_access_configuration(store: RegistryStore) -> None:
    with pytest.raises(ValueError, match="must be configured together"):
        dashboard_app_module.make_router(
            store,
            {"server": {"dashboard_access_team_domain": _TEAM_DOMAIN}},
        )
