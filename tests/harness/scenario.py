"""Load YAML scenario files and run them through the page-agent harness.

A file under ``tests/scenarios/`` is one scenario (a mapping) or a list of them.
Schema (all keys optional unless noted):

    name: str                 # test id; defaults to the file stem
    llm: "mock" | "live"      # default "mock"
    system_prompt: str        # overrides hub-default's prompt for this run
    page_tools:               # page-side MCP tools this fixture exposes
      - name: str             # required
        description: str
        result: str | mapping # canned tool result (default "")
    skill_results:            # stub these server skills with fixed text
      <skill name>: str
    turns:                    # required, non-empty
      - say: str              # required — the user utterance
        respond:              # mock only — what the fake LLM does this turn
          calls: [ str | {name: str, args: mapping} ]
          reply: str
        expect:
          reply_contains: str
          reply_matches: str          # re.search against the reply
          called: [str]               # page tools and/or skills that must run
          not_called: [str]
          args: { <tool>: mapping }   # exact args of the first call to <tool>
          images: int                 # number of images the reply carried

``live`` scenarios use the real configured LLM and are skipped unless
``AGENT_HUB_TEST_LIVE_LLM`` is set and ``llm.openai.api_key`` is configured.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_hub.config import load_config
from agent_hub.registry.store import RegistryStore
from tests.harness.page_agent_client import PageAgentClient, ToolHandler, Turn
from tests.harness.scripted_llm import ScriptedLLM, install_scripted_llm
from tests.harness.skill_spy import install_skill_spy

_SCENARIO_KEYS = {"name", "llm", "system_prompt", "page_tools", "skill_results", "turns"}
_TURN_KEYS = {"say", "respond", "expect"}
_RESPOND_KEYS = {"calls", "reply"}
_PAGE_TOOL_KEYS = {"name", "description", "result"}
_EXPECT_KEYS = {
    "reply_contains",
    "reply_matches",
    "called",
    "not_called",
    "args",
    "images",
}


class ScenarioError(Exception):
    """A scenario file is malformed."""


@dataclass
class Scenario:
    """One parsed scenario plus the id it runs under."""

    id: str
    spec: dict[str, Any]
    path: Path


def discover_scenarios(root: Path) -> list[Scenario]:
    """Return every scenario under ``root`` (``*.yaml`` / ``*.yml``), sorted."""
    paths = sorted({*root.rglob("*.yaml"), *root.rglob("*.yml")})
    found: list[Scenario] = []
    for path in paths:
        loaded = yaml.safe_load(path.read_text()) or {}
        items = loaded if isinstance(loaded, list) else [loaded]
        for index, spec in enumerate(items):
            if not isinstance(spec, dict):
                raise ScenarioError(f"{path.name}: scenario {index} is not a mapping")
            if spec.get("name"):
                scenario_id = str(spec["name"])
            elif len(items) == 1:
                scenario_id = path.stem
            else:
                scenario_id = f"{path.stem}[{index}]"
            found.append(Scenario(id=scenario_id, spec=spec, path=path))
    return found


async def run_scenario(
    scenario: Scenario,
    *,
    store: RegistryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execute one scenario, asserting each turn's expectations."""
    spec = scenario.spec
    _reject_unknown(spec, _SCENARIO_KEYS, f"scenario {scenario.id}")

    mode = str(spec.get("llm", "mock"))
    if mode not in {"mock", "live"}:
        raise ScenarioError(f"scenario {scenario.id}: llm must be 'mock' or 'live'")
    turns = spec.get("turns") or []
    if not isinstance(turns, list) or not turns:
        raise ScenarioError(f"scenario {scenario.id}: 'turns' must be a non-empty list")

    config: dict[str, Any] = {}
    if mode == "live":
        if not os.environ.get("AGENT_HUB_TEST_LIVE_LLM"):
            pytest.skip("live-LLM scenario; set AGENT_HUB_TEST_LIVE_LLM=1 to run")
        config = load_config()
        if not ((config.get("llm") or {}).get("openai") or {}).get("api_key"):
            pytest.skip("live-LLM scenario; no llm.openai.api_key configured")

    spy = install_skill_spy(monkeypatch, stub_results=spec.get("skill_results"))

    async with PageAgentClient.session(store, config=config) as page:
        for tool in spec.get("page_tools") or []:
            _reject_unknown(tool, _PAGE_TOOL_KEYS, f"scenario {scenario.id}: page_tools entry")
            name = str(tool["name"])
            page.add_tool(
                name,
                str(tool.get("description") or name),
                _canned(tool.get("result", "")),
            )
        await page.register()

        if spec.get("system_prompt") is not None:
            await store.update_persona("hub-default", system_prompt=str(spec["system_prompt"]))

        for index, turn in enumerate(turns):
            _reject_unknown(turn, _TURN_KEYS, f"scenario {scenario.id} turn {index}")
            if "say" not in turn:
                raise ScenarioError(f"scenario {scenario.id} turn {index}: missing 'say'")

            if mode == "mock":
                respond = turn.get("respond") or {}
                _reject_unknown(
                    respond, _RESPOND_KEYS, f"scenario {scenario.id} turn {index}: respond"
                )
                install_scripted_llm(
                    monkeypatch,
                    ScriptedLLM(
                        tool_calls=_tool_calls(respond.get("calls")),
                        reply=str(respond.get("reply", "")),
                    ),
                )

            before = len(spy.calls)
            result = await page.ask(str(turn["say"]))
            _assert_turn(
                scenario.id,
                index,
                turn.get("expect") or {},
                result,
                spy.calls[before:],
            )


def _canned(result: Any) -> ToolHandler:
    def handler(_args: dict[str, Any]) -> Any:
        return result

    return handler


def _tool_calls(raw: Any) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for entry in raw or []:
        if isinstance(entry, str):
            out.append((entry, {}))
        elif isinstance(entry, dict):
            out.append((str(entry["name"]), dict(entry.get("args") or {})))
        else:
            raise ScenarioError(f"tool call must be a string or mapping, got {entry!r}")
    return out


def _assert_turn(
    scenario_id: str,
    index: int,
    expect: dict[str, Any],
    turn: Turn,
    skill_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    where = f"{scenario_id} turn {index}"
    _reject_unknown(expect, _EXPECT_KEYS, f"{where}: expect")

    if "reply_contains" in expect:
        needle = str(expect["reply_contains"])
        assert needle in turn.reply, f"{where}: reply {turn.reply!r} has no {needle!r}"
    if "reply_matches" in expect:
        pattern = str(expect["reply_matches"])
        assert re.search(pattern, turn.reply), (
            f"{where}: reply {turn.reply!r} does not match /{pattern}/"
        )

    called = {call.name for call in turn.tool_calls} | {name for name, _ in skill_calls}
    for name in expect.get("called") or []:
        assert name in called, f"{where}: expected {name!r} to be called; called={sorted(called)}"
    for name in expect.get("not_called") or []:
        assert name not in called, f"{where}: {name!r} was called but should not have been"

    for name, want in (expect.get("args") or {}).items():
        got = turn.call_args(name)
        if got is None:
            got = next((args for n, args in skill_calls if n == name), None)
        assert got == want, f"{where}: {name} args {got!r} != {want!r}"

    if "images" in expect:
        assert len(turn.images) == int(expect["images"]), (
            f"{where}: expected {expect['images']} images, got {len(turn.images)}"
        )


def _reject_unknown(mapping: Any, allowed: set[str], where: str) -> None:
    if not isinstance(mapping, dict):
        raise ScenarioError(f"{where}: expected a mapping, got {type(mapping).__name__}")
    extra = set(mapping) - allowed
    if extra:
        raise ScenarioError(f"{where}: unknown keys {sorted(extra)}")


__all__ = ["Scenario", "ScenarioError", "discover_scenarios", "run_scenario"]
