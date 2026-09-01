"""Observe (and optionally stub) server-skill execution during a harness turn.

`/page-agent/ask` runs server skills (`get_current_time`, `get_weather`, …) in
process via `agent_hub.skills.run_result`, so they never reach the MCP bridge
and are invisible to :class:`PageAgentClient`. This patches that one function to
record every call and, for named skills, return a fixed result instead of
running the real one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

import agent_hub.skills as server_skills
from agent_hub.skills import SkillResult


@dataclass
class SkillSpy:
    """Records ``(name, args)`` for each server skill run this test."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def called(self, name: str) -> bool:
        return any(n == name for n, _ in self.calls)

    def call_args(self, name: str) -> dict[str, Any] | None:
        for n, args in self.calls:
            if n == name:
                return args
        return None


def install_skill_spy(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stub_results: dict[str, str] | None = None,
) -> SkillSpy:
    """Patch `agent_hub.skills.run_result` to record calls and stub named skills.

    Args:
        monkeypatch: The test's monkeypatch fixture.
        stub_results: ``{skill_name: text}`` — these skills return the given text
            without running; others run for real.

    Returns:
        A :class:`SkillSpy` accumulating calls for the duration of the test.
    """
    spy = SkillSpy()
    stubs = dict(stub_results or {})
    real_run_result = server_skills.run_result

    async def _wrapped(name: str, args: dict[str, Any]) -> SkillResult:
        spy.calls.append((name, dict(args)))
        if name in stubs:
            return SkillResult.success(stubs[name])
        return await real_run_result(name, args)

    monkeypatch.setattr("agent_hub.skills.run_result", _wrapped)
    return spy


__all__ = ["SkillSpy", "install_skill_spy"]
