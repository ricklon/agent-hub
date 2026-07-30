"""Skill: speak text aloud on a connected page agent.

Routes an LLM tool call to the page-agent MCP bridge, invoking
``page.audio_speaker.speak`` on whichever page agent is currently reachable.
This is the page-agent counterpart to a xiaozhi device's
``self.audio_speaker.set_volume`` — it makes the page a *talking* agent that
other agents (or the voice loop's tool policy) can drive.
"""

from __future__ import annotations

from typing import Any

from agent_hub.server import mcp_bridge
from agent_hub.skills import SkillResult

DEFINITION = {
    "type": "function",
    "function": {
        "name": "page_speak",
        "description": (
            "Speak text aloud through a connected browser page agent using the "
            "page's SpeechSynthesis. Call when the user asks to have something "
            "said out loud on the page agent."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The text to speak aloud.",
                }
            },
            "required": ["text"],
        },
    },
}


async def execute(args: dict[str, Any]) -> SkillResult:
    text = str(args.get("text") or "").strip()
    if not text:
        return SkillResult.failure("Text required.")
    handle = mcp_bridge.find_page_agent_for_tool("page.audio_speaker.speak")
    if handle is None:
        return SkillResult.failure("No page agent connected with page.audio_speaker.speak.")
    try:
        reply = await mcp_bridge.call_page_tool(
            handle.device_id, "page.audio_speaker.speak", {"text": text}
        )
    except Exception as exc:
        return SkillResult.failure(f"page_speak failed: {exc}", error=str(exc))
    return SkillResult.success(f"Page agent said: {reply}")
