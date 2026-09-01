"""Collector: turn every ``tests/scenarios/*.yaml`` into a parametrized test.

Parametrizing a normal async test over discovered files (rather than a custom
``pytest_collect_file``) keeps the ``store`` and ``monkeypatch`` fixtures
available with no extra machinery. Each scenario becomes one test node id'd by
its ``name``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_hub.registry.store import RegistryStore
from tests.harness import Scenario, discover_scenarios, run_scenario

_SCENARIOS = discover_scenarios(Path(__file__).parent)


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=[s.id for s in _SCENARIOS])
async def test_scenario(
    scenario: Scenario,
    store: RegistryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await run_scenario(scenario, store=store, monkeypatch=monkeypatch)
