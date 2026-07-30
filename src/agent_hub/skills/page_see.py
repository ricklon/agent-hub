"""Skill: capture a photo from a connected page agent's webcam.

Routes an LLM tool call to the page-agent MCP bridge, invoking
``page.camera.take_photo`` on a reachable page agent. This is the page-agent
counterpart to a xiaozhi device's ``self.camera.take_photo`` — it makes the
page a *seeing* agent. The returned JPEG data URL can be passed to a vision
model exactly like a device capture.
"""

from __future__ import annotations

from typing import Any

from agent_hub.server import mcp_bridge
from agent_hub.skills import SkillResult

DEFINITION = {
    "type": "function",
    "function": {
        "name": "page_see",
        "description": (
            "Capture one frame from a connected browser page agent's webcam and "
            "return it as a JPEG data URL. Call when the user asks the page agent "
            "to look at or photograph something."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}


async def execute(args: dict[str, Any]) -> SkillResult:
    handle = mcp_bridge.find_page_agent_for_tool("page.camera.take_photo")
    if handle is None:
        return SkillResult.failure("No page agent connected with page.camera.take_photo.")
    try:
        reply = await mcp_bridge.call_page_tool(handle.device_id, "page.camera.take_photo", {})
    except Exception as exc:
        return SkillResult.failure(f"page_see failed: {exc}", error=str(exc))
    if not reply.startswith("data:image"):
        return SkillResult.failure(f"page_see returned no image: {reply[:80]}")
    return SkillResult.success(reply)
