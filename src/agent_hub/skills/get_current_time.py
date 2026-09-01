"""Skill: return the current date and time in the server's configured zone."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_hub.config import load_settings
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


def _configured_tz() -> timezone | ZoneInfo:
    """Server timezone: IANA name (DST-aware) if set, else the fixed offset."""
    srv = load_settings().server
    name = srv.timezone.strip()
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return timezone(timedelta(hours=srv.timezone_offset))


def execute(args: dict[str, Any]) -> SkillResult:
    now = datetime.now(_configured_tz())
    return SkillResult.success(now.strftime("%A, %B %d, %Y — %I:%M %p %Z").strip())
