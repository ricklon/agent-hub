"""In-process test harness for exercising agents through the page-agent path.

See ``docs/agent-test-harness.md`` for the design. The pieces:

- :class:`PageAgentClient` — a protocol client that registers a synthetic page
  agent, drives ``/page-agent/ask``, and answers page-tool calls that arrive
  over the MCP bridge. No hardware, no browser, no network.
- :class:`ScriptedLLM` / :func:`install_scripted_llm` — a fake LLM provider for
  deterministic plumbing tests (tool routing, bridge, history, system prompt).
- :class:`SkillSpy` / :func:`install_skill_spy` — record and optionally stub
  server-skill execution, which otherwise runs in process unseen.
- :func:`discover_scenarios` / :func:`run_scenario` — the YAML scenario format
  and its runner (see ``tests/scenarios/``).
"""

from __future__ import annotations

from tests.harness.page_agent_client import (
    PageAgentClient,
    PageAgentError,
    ToolCall,
    ToolHandler,
    Turn,
)
from tests.harness.scenario import (
    Scenario,
    ScenarioError,
    discover_scenarios,
    run_scenario,
)
from tests.harness.scripted_llm import ScriptedLLM, install_scripted_llm
from tests.harness.skill_spy import SkillSpy, install_skill_spy

__all__ = [
    "PageAgentClient",
    "PageAgentError",
    "Scenario",
    "ScenarioError",
    "ScriptedLLM",
    "SkillSpy",
    "ToolCall",
    "ToolHandler",
    "Turn",
    "discover_scenarios",
    "install_scripted_llm",
    "install_skill_spy",
    "run_scenario",
]
