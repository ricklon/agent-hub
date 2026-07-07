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
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SkillResult:
    """Structured result returned by a server-side skill."""

    ok: bool
    text: str
    error: str | None = None

    @classmethod
    def success(cls, text: str) -> SkillResult:
        """Build a successful skill result."""
        return cls(ok=True, text=text)

    @classmethod
    def failure(cls, text: str, error: str | None = None) -> SkillResult:
        """Build a failed skill result with user-facing text."""
        return cls(ok=False, text=text, error=error or text)


_ExecutorResult = Awaitable[SkillResult | str] | SkillResult | str
_Executor = Callable[[dict[str, Any]], _ExecutorResult]

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
    """Execute a skill by name and return only user-facing text."""
    return (await run_result(name, args)).text


async def run_result(name: str, args: dict[str, Any]) -> SkillResult:
    """Execute a skill by name and return structured success/failure state."""
    item = _skills.get(name)
    if item is None:
        return SkillResult.failure(f"unknown skill: {name!r}")
    _, executor = item
    try:
        result = executor(args)
        if asyncio.iscoroutine(result):
            result = await result
    except Exception as exc:
        return SkillResult.failure(f"skill {name!r} failed: {exc}", error=str(exc))
    if isinstance(result, SkillResult):
        return result
    return SkillResult.success(str(result))


def has_skill(name: str) -> bool:
    return name in _skills
