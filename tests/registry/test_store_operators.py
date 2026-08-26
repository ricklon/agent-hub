"""Dashboard operator persistence and safety tests."""

from agent_hub.registry.models import OperatorRole
from agent_hub.registry.store import RegistryStore


async def test_operator_provisioning_defaults_to_viewer_and_tracks_email(
    store: RegistryStore,
) -> None:
    operator = await store.get_or_create_dashboard_operator(
        "subject-1", "Person@Example.COM", set()
    )
    updated = await store.get_or_create_dashboard_operator("subject-1", "new@example.com", set())

    assert operator.role == OperatorRole.VIEWER.value
    assert updated.id == operator.id
    assert updated.email == "new@example.com"
    assert len(await store.list_dashboard_operators()) == 1


async def test_configured_bootstrap_email_is_admin(store: RegistryStore) -> None:
    operator = await store.get_or_create_dashboard_operator(
        "subject-1", "Admin@Example.com", {"admin@example.com"}
    )

    assert operator.role == OperatorRole.ADMIN.value


async def test_bootstrap_config_promotes_existing_viewer(store: RegistryStore) -> None:
    await store.get_or_create_dashboard_operator("subject-1", "admin@example.com", set())
    promoted = await store.get_or_create_dashboard_operator(
        "subject-1", "admin@example.com", {"admin@example.com"}
    )

    assert promoted.role == OperatorRole.ADMIN.value


async def test_final_enabled_admin_cannot_be_removed(store: RegistryStore) -> None:
    await store.get_or_create_dashboard_operator(
        "subject-1", "admin@example.com", {"admin@example.com"}
    )

    demoted = await store.update_dashboard_operator("subject-1", OperatorRole.VIEWER, enabled=True)
    disabled = await store.update_dashboard_operator("subject-1", OperatorRole.ADMIN, enabled=False)

    assert demoted is False
    assert disabled is False


async def test_admin_can_manage_operator_when_another_admin_remains(
    store: RegistryStore,
) -> None:
    await store.get_or_create_dashboard_operator(
        "subject-1", "one@example.com", {"one@example.com"}
    )
    await store.get_or_create_dashboard_operator(
        "subject-2", "two@example.com", {"two@example.com"}
    )

    updated = await store.update_dashboard_operator(
        "subject-2", OperatorRole.OPERATOR, enabled=True
    )

    assert updated is True
