"""Dashboard audit middleware and timeline tests."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import agent_hub.dashboard.authorization as dashboard_auth_module
from agent_hub.dashboard.access_identity import AccessIdentityError, OperatorIdentity
from agent_hub.dashboard.app import make_router
from agent_hub.dashboard.audit import DashboardAuditMiddleware
from agent_hub.registry.store import RegistryStore

_ACCESS_CONFIG: dict[str, object] = {
    "server": {
        "dashboard_access_team_domain": "team.cloudflareaccess.com",
        "dashboard_access_audience": "app-audience",
        "dashboard_admin_emails": "admin@example.com",
    }
}


class _FakeVerifier:
    def __init__(self, team_domain: str, audience: str) -> None:
        assert team_domain == "team.cloudflareaccess.com"
        assert audience == "app-audience"

    async def verify(self, assertion: str) -> OperatorIdentity:
        identities = {
            "admin": OperatorIdentity(email="admin@example.com", subject="admin-subject"),
            "viewer": OperatorIdentity(email="viewer@example.com", subject="viewer-subject"),
        }
        try:
            return identities[assertion]
        except KeyError:
            raise AccessIdentityError("invalid") from None


def _app(store: RegistryStore, config: dict[str, object] | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(make_router(store, config or {}))
    app.add_middleware(DashboardAuditMiddleware, store=store)
    return app


async def test_successful_mutation_is_audited_without_form_values(
    store: RegistryStore,
) -> None:
    sensitive_model = "secret-provider/private-model-token"
    async with AsyncClient(
        transport=ASGITransport(app=_app(store)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/dashboard/models/select",
            data={"model_id": sensitive_model, "persona": "hub-default"},
        )

    assert response.status_code == 200
    events = await store.list_audit_events()
    assert len(events) == 1
    event = events[0]
    assert event.operator_subject is None
    assert event.operator_email == "local"
    assert event.operator_role == "admin"
    assert event.action == "models_select"
    assert event.outcome == "success"
    assert event.status_code == 200
    persisted_text = " ".join(
        value
        for value in (
            event.operator_subject,
            event.operator_email,
            event.operator_role,
            event.action,
            event.target_type,
            event.target_id,
            event.outcome,
        )
        if value is not None
    )
    assert sensitive_model not in persisted_text


async def test_failed_mutation_and_path_target_are_audited(store: RegistryStore) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=_app(store)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/dashboard/operators/missing-subject",
            data={"role": "operator", "enabled": "1"},
        )

    assert response.status_code == 409
    event = (await store.list_audit_events())[0]
    assert event.action == "operator_update"
    assert event.target_type == "operator"
    assert event.target_id == "missing-subject"
    assert event.outcome == "failure"
    assert event.status_code == 409


async def test_audit_timeline_is_admin_only_and_escapes_values(
    store: RegistryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dashboard_auth_module, "AccessIdentityVerifier", _FakeVerifier)
    await store.record_audit_event(
        operator_subject="unsafe-subject",
        operator_email="<script>alert(1)</script>@example.com",
        operator_role="operator",
        action="persona_update",
        target_type="persona",
        target_id="<unsafe>",
        outcome="success",
        status_code=200,
    )
    app = _app(store, _ACCESS_CONFIG)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admin = await client.get("/dashboard/audit", headers={"Cf-Access-Jwt-Assertion": "admin"})
        viewer = await client.get("/dashboard/audit", headers={"Cf-Access-Jwt-Assertion": "viewer"})

    assert admin.status_code == 200
    assert "Audit timeline" in admin.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;@example.com" in admin.text
    assert "persona: &lt;unsafe&gt;" in admin.text
    assert "<script>alert(1)</script>" not in admin.text
    assert viewer.status_code == 403


async def test_authenticated_viewer_denial_is_audited(
    store: RegistryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dashboard_auth_module, "AccessIdentityVerifier", _FakeVerifier)
    async with AsyncClient(
        transport=ASGITransport(app=_app(store, _ACCESS_CONFIG)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/dashboard/models/select",
            headers={"Cf-Access-Jwt-Assertion": "viewer"},
            data={"model_id": "example/model"},
        )

    assert response.status_code == 403
    event = (await store.list_audit_events())[0]
    assert event.operator_subject == "viewer-subject"
    assert event.operator_email == "viewer@example.com"
    assert event.operator_role == "viewer"
    assert event.action == "models_select"
    assert event.outcome == "failure"
    assert event.status_code == 403
