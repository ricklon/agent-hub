---
name: registry-model
description: Use when changing Agent Hub registry models, SQLite persistence, device metadata, persona binding, lifecycle status, conversation history, or any code under src/agent_hub/registry/.
---

# Registry model

Keep device identity stable, metadata current, and first contact immediately usable.

## Invariants

- Use SQLite for v1 and asynchronous SQLAlchemy access.
- Key physical devices by the normalized firmware `Device-Id`; never key by label, IP address, SSID, or client UUID.
- Auto-bind new devices to `hub-default`; do not add claim or activation requirements to first contact.
- Preserve lifecycle values and direction: `DISCOVERED → CLAIMED → ACTIVE → IDLE → OFFLINE`.
- Use `Agent.label` for a human-readable, mutable device name. Keep `device_id` visible as the stable identifier.
- Refresh mutable check-in metadata on every valid check-in, including label, firmware, IP address, and `last_seen`.
- Track liveness with `last_heartbeat`; a voice socket is also proof of life. Never infer health merely from registration.
- Keep health and activity independent: heartbeat freshness/faults determine health, while the voice pipeline determines activity.
- Never expose or log `websocket_token`.

## Schema changes

1. Inspect `models.py`, `store.py`, existing database initialization, and tests before editing.
2. Prefer an existing nullable column when its meaning fits; do not overload identity fields.
3. Ask before changing the data model. If a new column is necessary, provide an idempotent migration path because `create_all()` does not alter existing SQLite tables.
4. Add store tests covering creation and subsequent metadata refresh.
5. Update dashboard tests when a stored field changes presentation.
6. Run `just lint typecheck test`.

## Display and trust boundaries

- Escape all device-supplied metadata before rendering HTML.
- Show the friendly label first and the MAC/device ID alongside it.
- Derive online/offline presentation from live session state; do not rewrite persisted lifecycle state merely to alter a badge.
