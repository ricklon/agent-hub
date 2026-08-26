---
name: registry-model
description: Use when changing Agent Hub registry models, SQLite persistence, device metadata, persona binding, dashboard operators, lifecycle status, conversation history, or any code under src/agent_hub/registry/.
---

# Registry model

Keep identity stable, mutable metadata current, and first contact immediately usable. Read `src/agent_hub/registry/models.py`, `store.py`, initialization, and relevant tests before changing the schema; the code is authoritative.

## Device invariants

- Use SQLite for v1 and asynchronous SQLAlchemy access.
- Key physical devices by the normalized firmware `Device-Id`; never key by label, IP address, SSID, or client UUID.
- Auto-bind new devices to `hub-default`; do not add claim or activation requirements to first contact.
- Preserve lifecycle values and direction: `DISCOVERED → CLAIMED → ACTIVE → IDLE → OFFLINE`.
- Use `Agent.label` for a human-readable, mutable device name. Keep `device_id` visible as the stable identifier.
- Refresh mutable check-in metadata on every valid check-in, including label, firmware, IP address, and `last_seen`.
- Track liveness with `last_heartbeat`; a voice socket is also proof of life. Never infer health merely from registration.
- Keep health and activity independent: heartbeat freshness/faults determine health, while the voice pipeline determines activity.
- Never expose or log `websocket_token`.
- Scope conversation memory by `device_id`; never merge histories across devices or dashboard operators.

## Human operator invariants

- Key external identities by the stable verified provider subject. Email is mutable metadata and must not be the primary identity.
- New Cloudflare Access identities default to viewer unless their verified email is in the explicit bootstrap-admin configuration.
- Keep authorization and integrity safeguards in the store when every caller must obey them, including retaining at least one enabled administrator.
- Avoid writing last-seen state on every HTMX polling request; throttle persistence to prevent unnecessary SQLite contention.

## Schema changes

1. Ask before changing the data model.
2. Prefer an existing nullable column when its meaning fits; do not overload identity fields.
3. New tables may use `Base.metadata.create_all`. Existing-table changes need an idempotent migration in `_migrate()` because `create_all()` does not alter existing SQLite tables.
4. Keep `RegistryStore.initialize()` idempotent and protected by its lock because every port-specific app calls it concurrently.
5. Add store tests covering creation, subsequent metadata refresh, migrations, concurrency, and safety invariants affected by the change.
6. Update dashboard tests when a stored field changes presentation.
7. Run `just lint typecheck test`.

## Display and trust boundaries

- Escape all device- and identity-supplied metadata before rendering HTML.
- Show the friendly label first and the MAC/device ID alongside it.
- Derive online/offline presentation from live session state; do not rewrite persisted lifecycle state merely to alter a badge.
