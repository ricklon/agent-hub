"""Skill: return the current date and time."""

from datetime import datetime
from typing import Any

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


def execute(args: dict[str, Any]) -> str:
    return datetime.now().strftime("%A, %B %d, %Y — %I:%M %p")
