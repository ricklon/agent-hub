"""Skill: return the current date and time in the server's configured zone."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from agent_hub.config import configured_timezone
from agent_hub.skills import SkillResult

DEFINITION = {
    "type": "function",
    "function": {
        "name": "get_current_time",
        "description": (
            "Get the current local date and time. "
            "Call this whenever the user asks what time or date it is."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


def execute(args: dict[str, Any]) -> SkillResult:
    now = datetime.now(configured_timezone())
    return SkillResult.success(now.strftime("%A, %B %d, %Y — %I:%M %p %Z").strip())
