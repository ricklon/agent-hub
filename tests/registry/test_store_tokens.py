"""Tests for WebSocket token issue/validate/rotate in the registry store."""

from __future__ import annotations

from sqlalchemy import text

from agent_hub.registry.store import RegistryStore, hash_token

_DEVICE = "AA:BB:CC:DD:EE:FF"
_OTHER = "11:22:33:44:55:66"


class TestIssueWebsocketToken:
    async def test_unknown_device_gets_no_token(self, store):
        assert await store.issue_websocket_token("99:99:99:99:99:99") == ""

    async def test_issued_token_validates(self, store):
        await store.get_or_create_agent(_DEVICE)

        token = await store.issue_websocket_token(_DEVICE)

        assert token
        assert await store.validate_websocket_token(_DEVICE, token) is True

    async def test_each_device_gets_a_distinct_token(self, store):
        await store.get_or_create_agent(_DEVICE)
        await store.get_or_create_agent(_OTHER)

        first = await store.issue_websocket_token(_DEVICE)
        second = await store.issue_websocket_token(_OTHER)

        assert first != second
        assert await store.validate_websocket_token(_DEVICE, second) is False
        assert await store.validate_websocket_token(_OTHER, first) is False

    async def test_reissue_rotates_and_revokes_the_previous_token(self, store):
        await store.get_or_create_agent(_DEVICE)
        old = await store.issue_websocket_token(_DEVICE)

        new = await store.issue_websocket_token(_DEVICE)

        assert new != old
        assert await store.validate_websocket_token(_DEVICE, old) is False
        assert await store.validate_websocket_token(_DEVICE, new) is True

    async def test_issuing_bumps_last_seen(self, store):
        await store.get_or_create_agent(_DEVICE)
        created = await store.get_agent(_DEVICE)
        assert created is not None
        before = created.last_seen

        await store.issue_websocket_token(_DEVICE)

        refreshed = await store.get_agent(_DEVICE)
        assert refreshed is not None
        # Both reads come back from SQLite, so the tz representation matches.
        assert refreshed.last_seen >= before

    async def test_checkin_records_initial_liveness(self, store):
        agent = await store.get_or_create_agent(_DEVICE)

        assert agent.last_heartbeat is not None
        assert agent.reported_activity == "idle"


class TestValidateWebsocketToken:
    async def test_empty_token_never_validates(self, store):
        await store.get_or_create_agent(_DEVICE)
        await store.issue_websocket_token(_DEVICE)

        assert await store.validate_websocket_token(_DEVICE, "") is False

    async def test_unknown_device_never_validates(self, store):
        await store.get_or_create_agent(_DEVICE)
        token = await store.issue_websocket_token(_DEVICE)

        assert await store.validate_websocket_token("99:99:99:99:99:99", token) is False

    async def test_device_without_an_issued_token_never_validates(self, store):
        await store.get_or_create_agent(_DEVICE)

        assert await store.validate_websocket_token(_DEVICE, "guessed-token") is False

    async def test_wrong_token_is_rejected(self, store):
        await store.get_or_create_agent(_DEVICE)
        token = await store.issue_websocket_token(_DEVICE)

        assert await store.validate_websocket_token(_DEVICE, token + "x") is False
        assert await store.validate_websocket_token(_DEVICE, token[:-1]) is False


class TestTokenPersistence:
    async def test_initialize_migrates_default_persona_to_packaged_onnx_asr(self, tmp_path):
        db_path = tmp_path / "legacy-asr.db"
        first = RegistryStore(db_path=db_path)
        await first.initialize()
        await first.get_or_create_agent(_DEVICE)
        await first.update_persona("hub-default", asr_provider="funasr")
        await first._engine.dispose()

        second = RegistryStore(db_path=db_path)
        await second.initialize()
        try:
            persona = await second.get_persona_for_device(_DEVICE)
            assert persona is not None
            assert persona.asr_provider == "funasr_onnx"
        finally:
            await second._engine.dispose()

    async def test_token_survives_a_store_restart(self, tmp_path):
        db_path = tmp_path / "restart.db"
        first = RegistryStore(db_path=db_path)
        await first.initialize()
        await first.get_or_create_agent(_DEVICE)
        token = await first.issue_websocket_token(_DEVICE)
        await first._engine.dispose()

        second = RegistryStore(db_path=db_path)
        await second.initialize()
        try:
            assert await second.validate_websocket_token(_DEVICE, token) is True
        finally:
            await second._engine.dispose()

    async def test_migration_adds_the_column_to_a_pre_existing_database(self, tmp_path):
        # Devices already in the field have rows written before the
        # websocket_token column existed; re-running initialize() must migrate
        # them in place rather than fail or drop them.
        db_path = tmp_path / "legacy.db"
        first = RegistryStore(db_path=db_path)
        await first.initialize()
        await first.get_or_create_agent(_DEVICE, firmware_version="2.2.6")
        await first._engine.dispose()

        second = RegistryStore(db_path=db_path)
        await second.initialize()
        try:
            agent = await second.get_agent(_DEVICE)
            assert agent is not None
            assert agent.firmware_version == "2.2.6"
            assert agent.websocket_token is None

            token = await second.issue_websocket_token(_DEVICE)
            assert await second.validate_websocket_token(_DEVICE, token) is True
        finally:
            await second._engine.dispose()


async def _stored_token(store: RegistryStore, device_id: str) -> str | None:
    agent = await store.get_agent(device_id)
    assert agent is not None
    return agent.websocket_token


class TestTokensAreStoredHashed:
    async def test_database_never_holds_the_token(self, store):
        await store.get_or_create_agent(_DEVICE)
        token = await store.issue_websocket_token(_DEVICE)

        stored = await _stored_token(store, _DEVICE)
        assert stored == hash_token(token)
        assert token not in (stored or "")

    async def test_the_stored_hash_is_not_a_valid_token(self, store):
        await store.get_or_create_agent(_DEVICE)
        await store.issue_websocket_token(_DEVICE)
        stored = await _stored_token(store, _DEVICE)
        assert stored

        # Someone who copied the database can't present what they found.
        assert await store.validate_websocket_token(_DEVICE, stored) is False
        assert (
            await store.record_authenticated_heartbeat(_DEVICE, stored, None, "idle", []) is False
        )
        assert await store.mark_agent_offline(_DEVICE, stored) is False

    async def test_heartbeat_and_offline_accept_the_issued_token(self, store):
        await store.get_or_create_agent(_DEVICE)
        token = await store.issue_websocket_token(_DEVICE)

        assert await store.record_authenticated_heartbeat(_DEVICE, token, None, "idle", []) is True
        assert await store.mark_agent_offline(_DEVICE, token) is True


class TestLegacyPlainTextTokens:
    async def _plant_legacy_token(self, store: RegistryStore, token: str) -> None:
        """Write a token the way stores did before hashing."""
        await store.get_or_create_agent(_DEVICE)
        async with store._sessions() as session:
            await session.execute(
                text("UPDATE agents SET websocket_token = :t WHERE device_id = :d"),
                {"t": token, "d": _DEVICE},
            )
            await session.commit()

    async def test_startup_hashes_them_and_the_agent_keeps_working(self, tmp_path):
        store = RegistryStore(db_path=tmp_path / "legacy.db")
        await store.initialize()
        await self._plant_legacy_token(store, "legacy-token")
        await store._engine.dispose()

        restarted = RegistryStore(db_path=tmp_path / "legacy.db")
        await restarted.initialize()
        try:
            assert await _stored_token(restarted, _DEVICE) == hash_token("legacy-token")
            assert await restarted.validate_websocket_token(_DEVICE, "legacy-token") is True
        finally:
            await restarted._engine.dispose()

    async def test_hashing_twice_changes_nothing(self, store):
        await self._plant_legacy_token(store, "legacy-token")

        await store._hash_legacy_tokens()
        once = await _stored_token(store, _DEVICE)
        await store._hash_legacy_tokens()

        assert await _stored_token(store, _DEVICE) == once
        assert await store.validate_websocket_token(_DEVICE, "legacy-token") is True
