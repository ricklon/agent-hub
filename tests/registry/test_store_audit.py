"""Audit event persistence tests."""

from agent_hub.registry.store import RegistryStore


async def test_audit_events_are_returned_newest_first(store: RegistryStore) -> None:
    await store.record_audit_event(
        operator_subject="subject-1",
        operator_email="operator@example.com",
        operator_role="operator",
        action="first_action",
        target_type="agent",
        target_id="device-1",
        outcome="success",
        status_code=200,
    )
    await store.record_audit_event(
        operator_subject=None,
        operator_email="local",
        operator_role="admin",
        action="second_action",
        target_type=None,
        target_id=None,
        outcome="failure",
        status_code=409,
    )

    events = await store.list_audit_events()

    assert [event.action for event in events] == ["second_action", "first_action"]
    assert events[0].operator_subject is None
    assert events[0].operator_email == "local"
    assert events[0].outcome == "failure"
    assert events[0].status_code == 409
    assert events[1].target_type == "agent"
    assert events[1].target_id == "device-1"


async def test_audit_event_limit_is_enforced(store: RegistryStore) -> None:
    for index in range(3):
        await store.record_audit_event(
            operator_subject=None,
            operator_email="local",
            operator_role="admin",
            action=f"action_{index}",
            target_type=None,
            target_id=None,
            outcome="success",
            status_code=200,
        )

    events = await store.list_audit_events(limit=2)

    assert [event.action for event in events] == ["action_2", "action_1"]
