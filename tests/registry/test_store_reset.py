"""Tests for the bulk reset helpers used by `just reset-data`."""

from __future__ import annotations

_DEVICE = "AA:BB:CC:DD:EE:FF"
_OTHER = "11:22:33:44:55:66"


class TestConversationTurnCount:
    async def test_empty_store_counts_zero(self, store):
        assert await store.conversation_turn_count() == 0

    async def test_counts_messages_across_devices(self, store):
        await store.append_history(_DEVICE, "user", "hello")
        await store.append_history(_DEVICE, "assistant", "hi there")
        await store.append_history(_OTHER, "user", "different device")

        assert await store.conversation_turn_count() == 3


class TestClearAllHistory:
    async def test_removes_every_device_and_reports_count(self, store):
        await store.append_history(_DEVICE, "user", "one")
        await store.append_history(_DEVICE, "assistant", "two")
        await store.append_history(_OTHER, "user", "three")

        removed = await store.clear_all_history()

        assert removed == 3
        assert await store.conversation_turn_count() == 0
        assert await store.load_history(_DEVICE) == []
        assert await store.load_history(_OTHER) == []

    async def test_is_a_no_op_on_an_empty_store(self, store):
        assert await store.clear_all_history() == 0

    async def test_keeps_agents_and_personas(self, store):
        await store.get_or_create_agent(_DEVICE)
        await store.append_history(_DEVICE, "user", "hello")

        await store.clear_all_history()

        assert await store.get_agent(_DEVICE) is not None
        assert await store.get_persona_for_device(_DEVICE) is not None


class TestClearLLMSpend:
    async def test_removes_ledger_and_reports_count(self, store):
        await store.record_llm_spend("gemma", 10, 5, 0.001, False, device_id=_DEVICE)
        await store.record_llm_spend("gemma", 20, 8, 0.002, False, device_id=None)

        removed = await store.clear_llm_spend()

        assert removed == 2
        summary = await store.llm_spend_summary()
        assert summary["calls"] == 0
        assert summary["cost_usd"] == 0.0

    async def test_is_a_no_op_on_an_empty_ledger(self, store):
        assert await store.clear_llm_spend() == 0

    async def test_leaves_conversation_history_untouched(self, store):
        await store.append_history(_DEVICE, "user", "hello")
        await store.record_llm_spend("gemma", 10, 5, 0.001, False, device_id=_DEVICE)

        await store.clear_llm_spend()

        assert await store.conversation_turn_count() == 1
