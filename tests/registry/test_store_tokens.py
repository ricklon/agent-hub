"""Tests for WebSocket token issue/validate/rotate in the registry store."""

from __future__ import annotations

from agent_hub.registry.store import RegistryStore

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
