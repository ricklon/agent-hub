"""Tests for LLM spend metering, the warning threshold, and the hard limit."""

from __future__ import annotations

import pytest

from agent_hub import spend
from agent_hub.registry.store import RegistryStore
from agent_hub.spend import SpendConfig, SpendLimitExceeded, SpendTracker


async def _store(tmp_path) -> RegistryStore:
    store = RegistryStore(db_path=tmp_path / "registry.db")
    await store.initialize()
    return store


class TestRecording:
    async def test_reported_cost_is_stored_verbatim(self, tmp_path):
        store = await _store(tmp_path)
        tracker = SpendTracker(store, SpendConfig())

        await tracker.record("gpt-4o-mini", 1000, 500, cost_usd=0.0123)

        summary = await store.llm_spend_summary()
        assert summary["cost_usd"] == pytest.approx(0.0123)
        assert summary["prompt_tokens"] == 1000
        assert summary["completion_tokens"] == 500
        assert summary["calls"] == 1
        assert summary["estimated_calls"] == 0

    async def test_unreported_cost_falls_back_to_the_price_table(self, tmp_path):
        store = await _store(tmp_path)
        config = SpendConfig.from_config(
            {"llm": {"spend": {"pricing": {"local/model": {"input": 1.0, "output": 2.0}}}}}
        )
        tracker = SpendTracker(store, config)

        # 1M input @ $1 + 0.5M output @ $2 = $2.00
        await tracker.record("local/model", 1_000_000, 500_000, cost_usd=None)

        summary = await store.llm_spend_summary()
        assert summary["cost_usd"] == pytest.approx(2.0)
        # Flagged so the dashboard does not present an estimate as billing truth.
        assert summary["estimated_calls"] == 1

    async def test_unpriced_model_records_tokens_but_no_cost(self, tmp_path):
        store = await _store(tmp_path)
        tracker = SpendTracker(store, SpendConfig())

        await tracker.record("mystery/model", 100, 50, cost_usd=None)

        summary = await store.llm_spend_summary()
        assert summary["cost_usd"] == pytest.approx(0.0)
        assert summary["prompt_tokens"] == 100

    async def test_spend_is_broken_down_per_model(self, tmp_path):
        store = await _store(tmp_path)
        tracker = SpendTracker(store, SpendConfig())

        await tracker.record("cheap", 1, 1, cost_usd=0.01)
        await tracker.record("pricey", 1, 1, cost_usd=0.50)
        await tracker.record("cheap", 1, 1, cost_usd=0.01)

        rows = await store.llm_spend_by_model()
        assert [r["model"] for r in rows] == ["pricey", "cheap"]
        assert rows[1]["calls"] == 2


class TestLimits:
    async def test_no_limits_configured_never_blocks(self, tmp_path):
        store = await _store(tmp_path)
        tracker = SpendTracker(store, SpendConfig())

        await tracker.record("m", 1, 1, cost_usd=1000.0)

        await tracker.guard()  # must not raise

    async def test_guard_blocks_once_the_total_cap_is_reached(self, tmp_path):
        store = await _store(tmp_path)
        tracker = SpendTracker(store, SpendConfig(total_limit_usd=1.0))

        await tracker.record("m", 1, 1, cost_usd=0.99)
        await tracker.guard()  # still under

        await tracker.record("m", 1, 1, cost_usd=0.02)
        with pytest.raises(SpendLimitExceeded) as excinfo:
            await tracker.guard()

        assert excinfo.value.window == "total"
        assert excinfo.value.limit_usd == 1.0

    async def test_daily_and_total_caps_are_independent(self, tmp_path):
        store = await _store(tmp_path)
        # Daily is the tighter cap, so it trips first.
        tracker = SpendTracker(store, SpendConfig(daily_limit_usd=0.5, total_limit_usd=100.0))

        await tracker.record("m", 1, 1, cost_usd=0.60)

        with pytest.raises(SpendLimitExceeded) as excinfo:
            await tracker.guard()
        assert excinfo.value.window == "daily"

    async def test_warning_fires_below_the_cap_without_blocking(self, tmp_path, caplog):
        store = await _store(tmp_path)
        tracker = SpendTracker(store, SpendConfig(total_limit_usd=1.0, warn_at=0.8))

        await tracker.record("m", 1, 1, cost_usd=0.85)
        await tracker.guard()  # warns, does not raise

        totals = await tracker.totals()
        assert totals["utilisation"]["total"] == pytest.approx(0.85)
        assert totals["blocked"] is False

    async def test_totals_reports_disabled_caps_as_none(self, tmp_path):
        store = await _store(tmp_path)
        tracker = SpendTracker(store, SpendConfig(total_limit_usd=2.0))

        await tracker.record("m", 1, 1, cost_usd=1.0)

        totals = await tracker.totals()
        assert totals["utilisation"]["daily"] is None
        assert totals["utilisation"]["total"] == pytest.approx(0.5)


class TestConfigParsing:
    def test_env_override_strings_are_coerced_to_numbers(self):
        """The droplet sets these via env, so values arrive as strings."""
        config = SpendConfig.from_config(
            {
                "llm": {
                    "spend": {
                        "daily_limit_usd": "5",
                        "total_limit_usd": "25",
                        "warn_at": "0.9",
                    }
                }
            }
        )

        assert config.daily_limit_usd == pytest.approx(5.0)
        assert config.total_limit_usd == pytest.approx(25.0)
        assert config.warn_at == pytest.approx(0.9)

    def test_missing_section_yields_disabled_caps(self):
        config = SpendConfig.from_config({})

        assert config.daily_limit_usd == 0.0
        assert config.total_limit_usd == 0.0
        assert config.prices == {}


class TestModuleLevelWiring:
    async def test_guard_and_record_are_noops_until_configured(self, tmp_path):
        spend.reset()
        try:
            await spend.guard()
            await spend.record("m", 1, 1, cost_usd=1.0)
        finally:
            spend.reset()

    async def test_configure_installs_a_tracker_that_enforces(self, tmp_path):
        store = await _store(tmp_path)
        spend.reset()
        try:
            spend.configure(store, {"llm": {"spend": {"total_limit_usd": 0.5}}})

            await spend.record("m", 1, 1, cost_usd=0.75)
            with pytest.raises(SpendLimitExceeded):
                await spend.guard()
        finally:
            spend.reset()


async def test_record_attributes_spend_to_the_bound_agent(store) -> None:
    """Any LLM call made inside a task bound with bind_device lands on that agent."""
    from agent_hub import spend

    spend.reset()
    spend.configure(store, {"llm": {"spend": {}}})
    try:
        spend.bind_device("page-abc")
        await spend.record("m", 10, 5, 0.01)
        spend.bind_device(None)
        await spend.record("m", 10, 5, 0.02)
        await spend.record("m", 10, 5, 0.03, device_id="dev-1")
    finally:
        spend.reset()
    by_device = await store.llm_spend_by_device()
    assert by_device["page-abc"]["calls"] == 1
    assert by_device[""]["calls"] == 1
    assert by_device["dev-1"]["cost_usd"] == 0.03
