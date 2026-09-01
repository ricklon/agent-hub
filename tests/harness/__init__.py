"""In-process test harness for exercising agents through the page-agent path.

See ``docs/agent-test-harness.md`` for the design. The pieces:

- :class:`PageAgentClient` — a protocol client that registers a synthetic page
  agent, drives ``/page-agent/ask``, and answers page-tool calls that arrive
  over the MCP bridge. No hardware, no browser, no network.
- :class:`ScriptedLLM` / :func:`install_scripted_llm` — a fake LLM provider for
  deterministic plumbing tests (tool routing, bridge, history, system prompt).
"""

from __future__ import annotations

from tests.harness.page_agent_client import (
    PageAgentClient,
    PageAgentError,
    ToolCall,
    Turn,
)
from tests.harness.scripted_llm import ScriptedLLM, install_scripted_llm

__all__ = [
    "PageAgentClient",
    "PageAgentError",
    "ScriptedLLM",
    "ToolCall",
    "Turn",
    "install_scripted_llm",
]
