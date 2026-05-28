"""Server-side skills: auto-discovered tools available to all agents.

Each skill module must define:
  DEFINITION: dict  — OpenAI function-calling schema
  execute(args: dict) -> str  — async executor (or sync, both work)

Skills are discovered at import time from this package directory.
Any module starting with '_' is skipped.
"""

from __future__ import annotations

import asyncio
import importlib
import pkgutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

_Executor = Callable[[dict[str, Any]], Awaitable[str] | str]

_skills: dict[str, tuple[dict[str, Any], _Executor]] = {}


def _load() -> None:
    pkg_dir = Path(__file__).parent
    for info in pkgutil.iter_modules([str(pkg_dir)]):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"agent_hub.skills.{info.name}")
        if hasattr(mod, "DEFINITION") and hasattr(mod, "execute"):
            name: str = mod.DEFINITION["function"]["name"]
            _skills[name] = (mod.DEFINITION, mod.execute)


_load()


def get_definitions() -> list[dict[str, Any]]:
    """Return all skill definitions in OpenAI function-calling format."""
    return [defn for defn, _ in _skills.values()]


async def run(name: str, args: dict[str, Any]) -> str:
    """Execute a skill by name. Returns an error string if not found."""
    item = _skills.get(name)
    if item is None:
        return f"unknown skill: {name!r}"
    _, executor = item
    result = executor(args)
    if asyncio.iscoroutine(result):
        awaited = await result
        return str(awaited)
    return str(result)


def has_skill(name: str) -> bool:
    return name in _skills
