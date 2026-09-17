"""Stale-agent cleanup shared by the dashboard and the background sweeper.

Every device that ever checked in and every browser tab that ever opened the
page agent leaves a registry row. Boards come back; per-tab page agents do not.
The policy therefore has two thresholds: a long one for devices (a board off
for the weekend is not stale) and a short one for per-tab page agents (a tab
closed yesterday is). A named page agent is meant to be reopened, so it is
treated like a device. The dashboard offers the same sweep as a button; the
server runs it on a timer for per-tab page agents only, so nothing meant to
come back is removed without a human.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from loguru import logger

from agent_hub.registry.models import Agent, AgentKind
from agent_hub.registry.page_identity import is_named_page_agent
from agent_hub.registry.store import RegistryStore
from agent_hub.server import mcp_bridge, session_state

_TAG = "cleanup"


@dataclass(frozen=True)
class StalePolicy:
    """How long an agent may go unseen before it counts as stale."""

    device_after: timedelta = timedelta(days=14)
    page_after: timedelta = timedelta(hours=24)

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> StalePolicy:
        """Read ``registry.stale_device_days`` / ``registry.stale_page_hours``."""
        reg = config.get("registry") or {}
        return cls(
            device_after=timedelta(days=float(reg.get("stale_device_days", 14))),
            page_after=timedelta(hours=float(reg.get("stale_page_hours", 24))),
        )

    def describe(self) -> str:
        """Human-readable thresholds for the dashboard."""
        return (
            f"devices and named page agents unseen for {_fmt(self.device_after)}, "
            f"per-tab page agents unseen for {_fmt(self.page_after)}"
        )


def _fmt(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    if hours >= 48 and hours % 24 == 0:
        return f"{int(hours // 24)} days"
    return f"{int(hours)} hours" if hours != 1 else "1 hour"


async def find_stale(store: RegistryStore, policy: StalePolicy) -> list[Agent]:
    """Agents past their kind's threshold that have no live connection now."""
    stale = await store.list_stale_agents(
        device_after=policy.device_after, page_after=policy.page_after
    )
    return [a for a in stale if not _is_live(a.device_id)]


def _is_live(device_id: str) -> bool:
    if session_state.is_connected(device_id):
        return True
    handle = mcp_bridge.get_page_agent(device_id)
    return handle is not None and handle.connected


async def remove_agent(store: RegistryStore, device_id: str, *, keep_history: bool = False) -> bool:
    """Delete one agent everywhere: registry row, bridge, live state, and history.

    Args:
        store: Registry store.
        device_id: The agent to remove.
        keep_history: Leave its conversation history, so the agent picks it up
            if it comes back with the same id.
    """
    mcp_bridge.unregister_page_agent(device_id)
    session_state.set_pipeline_status(device_id, "idle")
    return await store.delete_agent(device_id, keep_history=keep_history)


async def prune(
    store: RegistryStore,
    policy: StalePolicy,
    *,
    select: Callable[[Agent], bool] | None = None,
) -> list[str]:
    """Remove every stale agent (optionally only those ``select`` accepts).

    Args:
        store: Registry store.
        policy: Staleness thresholds.
        select: Only remove agents this returns True for; None means all.

    Returns:
        Device ids that were removed.
    """
    removed: list[str] = []
    for agent in await find_stale(store, policy):
        if select is not None and not select(agent):
            continue
        if await remove_agent(store, agent.device_id):
            removed.append(agent.device_id)
    if removed:
        logger.bind(tag=_TAG).info(f"Pruned {len(removed)} stale agent(s): {removed}")
    return removed


def is_per_tab_page(agent: Agent) -> bool:
    """A page agent that nobody can reopen by name: safe to sweep automatically."""
    return agent.kind == AgentKind.PAGE.value and not is_named_page_agent(agent)
