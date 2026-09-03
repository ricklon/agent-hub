"""One text turn for any bridged agent — page agent, robot, or other MCP agent.

This is the loop that ``/page-agent/ask`` used to own inline: gather the
agent's own MCP tools plus server skills plus any borrowed linked-agent
tools, run the persona's LLM with function calling, route each tool call
back to the agent over the MCP bridge, and record the result.

It lives here rather than in ``server.page_agent`` because a browser page is
only one kind of bridged agent. A robot that registers through
``server.agent_api`` gets the same turn, and so does the dashboard's agent
console — which is what makes "test my robot" and "talk to my robot" the
same code path rather than two that drift.

The device voice loop in ``server.ws_session`` still has its own copy; see
``docs/agent-management-plan.md`` for the plan to fold it in here too.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from agent_hub import skills as server_skills
from agent_hub import spend
from agent_hub.providers.llm import get_provider
from agent_hub.registry.models import Persona
from agent_hub.registry.store import RegistryStore
from agent_hub.server import mcp_bridge, session_state
from agent_hub.server.tool_policy import is_risky_tool

_TAG = "agent_turn"

# Server skills that are wrappers for driving *another* agent's page. A
# bridged agent calls its own tools directly, so offering these to it would
# let it talk to itself the long way round.
_WRAPPER_SKILLS = frozenset({"page_speak", "page_see"})

# Borrowed tools are namespaced with the source agent id: ``robot-01.grip``.
_LINKED_SEP = "."


class TurnError(RuntimeError):
    """The turn could not run (no persona, LLM failure, unknown agent)."""


@dataclass
class TurnResult:
    """What one text turn produced."""

    reply: str
    images: list[str] = field(default_factory=list)
    tools_called: list[str] = field(default_factory=list)
    llm_ms: int = 0


# ── Linked-agent tools ───────────────────────────────────────────────────────


def linked_tool_defs(persona: Persona) -> list[dict[str, Any]]:
    """OpenAI tool defs for the borrowable tools of every linked agent."""
    out: list[dict[str, Any]] = []
    for linked_id in persona.linked_agents_list:
        for d in mcp_bridge.list_page_tool_definitions(linked_id):
            fn = d["function"]
            if is_risky_tool(fn["name"], mcp_bridge.tool_annotations(linked_id, fn["name"])):
                continue
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": f"{linked_id}{_LINKED_SEP}{fn['name']}",
                        "description": f"[{linked_id}] {fn['description']}",
                        "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
                    },
                }
            )
    return out


def resolve_linked_call(persona: Persona, name: str) -> tuple[str, str] | None:
    """Split a namespaced linked-tool name into ``(agent_id, tool)``, or None."""
    for linked_id in persona.linked_agents_list:
        prefix = f"{linked_id}{_LINKED_SEP}"
        if name.startswith(prefix):
            return linked_id, name[len(prefix) :]
    return None


async def call_linked_tool(linked_id: str, tool: str, args: dict[str, Any]) -> str:
    """Run a borrowed tool on a linked agent; never raises."""
    handle = mcp_bridge.get_page_agent(linked_id)
    if handle is None or not handle.connected:
        return f"{linked_id} is not connected — cannot run {tool!r}."
    try:
        return await mcp_bridge.call_page_tool(linked_id, tool, args, timeout=30.0)
    except Exception as exc:  # noqa: BLE001 - surface any bridge failure to the model
        return f"{linked_id}{_LINKED_SEP}{tool} failed: {exc}"


# ── The turn ─────────────────────────────────────────────────────────────────


def tool_timeout(name: str) -> float:
    """Seconds to allow one tool call. Cameras need to wake up and focus."""
    return 60.0 if ("camera" in name or "photo" in name) else 30.0


def build_system_prompt(persona: Persona, tools: list[dict[str, Any]]) -> str:
    """Persona prompt plus the tool menu, in the shape the voice loop uses."""
    lines: list[str] = []
    for d in tools:
        fn = d["function"]
        extra = ""
        if "camera" in fn["name"] or "photo" in fn["name"]:
            extra = " Always pass a 'question' arg describing what to look for."
        lines.append(f"- {fn['name']}: {fn['description']}{extra}")
    prompt = persona.system_prompt or ""
    if lines:
        prompt = (
            f"{prompt}\n\nAvailable tools you MUST use when relevant:\n" + "\n".join(lines)
        ).strip()
    return prompt


def agent_tool_defs(device_id: str, persona: Persona) -> list[dict[str, Any]]:
    """Every tool this agent may call: its own, the server's, and borrowed ones."""
    own = mcp_bridge.list_page_tool_definitions(device_id)
    skills = [
        d for d in server_skills.get_definitions() if d["function"]["name"] not in _WRAPPER_SKILLS
    ]
    return own + skills + linked_tool_defs(persona)


async def run_turn(
    store: RegistryStore,
    config: dict[str, Any],
    device_id: str,
    text: str,
) -> TurnResult:
    """Run one text turn for a bridged agent and persist it to history.

    Args:
        store: Registry store (persona lookup and conversation history).
        config: Raw config dict, for the LLM provider.
        device_id: The bridged agent answering.
        text: What the user said.

    Returns:
        The reply, any images captured by tools, and which tools ran.

    Raises:
        TurnError: No persona is assigned, or the LLM call failed.
    """
    persona = await store.get_persona_for_device(device_id)
    if persona is None:
        raise TurnError(f"no persona assigned to {device_id!r}")

    tools = agent_tool_defs(device_id, persona)
    own_tool_names = {
        d["function"]["name"] for d in mcp_bridge.list_page_tool_definitions(device_id)
    }

    history = await store.load_history(device_id, limit=persona.memory_window * 2)
    history.append({"role": "user", "content": text})
    system_prompt = build_system_prompt(persona, tools)

    images: list[str] = []
    called: list[str] = []

    async def _exec_tool(name: str, args: dict[str, Any]) -> str:
        called.append(name)
        linked = resolve_linked_call(persona, name)
        if linked is not None:
            return await call_linked_tool(linked[0], linked[1], args)
        if name in own_tool_names:
            result = await mcp_bridge.call_page_tool(
                device_id, name, args, timeout=tool_timeout(name)
            )
            if isinstance(result, str) and result.startswith("data:image"):
                images.append(result)
            return result
        if server_skills.has_skill(name):
            return (await server_skills.run_result(name, args)).text
        return f"unknown tool: {name!r}"

    llm = get_provider(persona.llm_provider, config, model_override=persona.llm_model or None)
    spend.bind_device(device_id)
    session_state.set_pipeline_status(device_id, "thinking", text)
    started = time.monotonic()
    try:
        reply = await llm.complete_with_tools(
            history, tools, _exec_tool, system_prompt=system_prompt
        )
    except Exception as exc:
        session_state.set_pipeline_status(device_id, "idle")
        logger.bind(tag=_TAG).error(f"Turn failed for {device_id!r}: {exc}")
        raise TurnError(str(exc)) from exc
    llm_ms = int((time.monotonic() - started) * 1000)
    session_state.set_pipeline_status(device_id, "idle")
    session_state.record_turn(device_id, 0, llm_ms, 0)

    reply = (reply or "").strip()
    if reply:
        await store.append_history(device_id, "user", text)
        await store.append_history(
            device_id, "assistant", f"{reply}\n[image:captured]" if images else reply
        )
    logger.bind(tag=_TAG).info(
        f"Turn {device_id!r}: {text!r} → {reply[:80]!r} "
        f"({len(called)} tool calls, {len(images)} images)"
    )
    return TurnResult(reply=reply, images=images, tools_called=called, llm_ms=llm_ms)


async def call_one_tool(
    device_id: str,
    name: str,
    args: dict[str, Any],
) -> str:
    """Call a single tool on a bridged agent directly, with no LLM in the loop.

    This is the manual test path: a builder poking one tool on their robot to
    see what it does, without spending a model call or hoping the model picks
    the tool they meant.

    Raises:
        TurnError: The agent is not connected, or the tool reported an error.
    """
    handle = mcp_bridge.get_page_agent(device_id)
    if handle is None:
        raise TurnError(f"{device_id} has never registered its tools")
    if not handle.connected:
        raise TurnError(f"{device_id} is not connected right now")
    if name not in handle.tools:
        raise TurnError(f"{device_id} does not expose a tool called {name!r}")
    try:
        return await mcp_bridge.call_page_tool(device_id, name, args, timeout=tool_timeout(name))
    except Exception as exc:
        raise TurnError(str(exc)) from exc
