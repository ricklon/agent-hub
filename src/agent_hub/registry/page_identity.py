"""Identity of named page agents, shared by registration, the store and cleanup.

A page agent registered with a name gets an id derived from its owner and that
name, so reopening the name is the same agent. Anything that needs to tell a
named, long-lived page agent from a per-tab one derives the id again and
compares, rather than trusting a flag the browser could have set.
"""

from __future__ import annotations

import hashlib

from agent_hub.registry.models import Agent, AgentKind

# Owner of page agents on a hub without Cloudflare Access, where there are no
# verified users to tell apart.
LOCAL_OWNER = "local"


def named_page_device_id(owner_key: str, name: str) -> str:
    """Stable page agent id for one owner's named agent.

    Deterministic, so reopening a name finds the same row and its history;
    scoped to the owner, so two people can each have a "kitchen". Names match
    case-insensitively: "Kitchen" and "kitchen" are the same agent.

    Args:
        owner_key: The verified Access subject, or ``LOCAL_OWNER``.
        name: The agent's name as the owner typed it.
    """
    digest = hashlib.sha256(f"{owner_key}\n{name.casefold()}".encode()).hexdigest()
    return "page-" + digest[:16]


def is_named_page_agent(agent: Agent) -> bool:
    """True for a page agent whose id is the named id for its owner and label.

    Per-tab page agents (random ids, from before names or from an unnamed
    registration) are False, as is every other kind of agent.
    """
    if agent.kind != AgentKind.PAGE.value or not agent.label:
        return False
    if agent.owner_subject:
        owner_key = agent.owner_subject
    elif agent.owner == LOCAL_OWNER:
        owner_key = LOCAL_OWNER
    else:
        return False
    return agent.device_id == named_page_device_id(owner_key, agent.label)
