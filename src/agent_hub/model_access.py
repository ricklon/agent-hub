"""Who may run paid models, and what a turn runs when its owner may not.

In free mode (``llm.free_only``) a person's agents run free models unless an
admin has turned on paid models for them; admins always may. Personas are
shared, so the check happens when a turn runs, against the agent's owner:
a paid-allowed user putting a paid model on ``hub-default`` must not make
everyone else's agents start spending. An agent nobody owns (an unclaimed
device, a local page agent) follows the hub default, which is free.

A turn that may not use its persona's paid model runs on a free substitute
instead: the first ``:free`` model in ``llm.openai.fallback_models``, or the
hub default model if that is free. With neither, the turn is refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_hub.config import config_bool
from agent_hub.providers.llm import LLMProvider, get_provider, model_list, resolved_model
from agent_hub.registry.models import Agent, OperatorRole, Persona
from agent_hub.registry.store import RegistryStore
from agent_hub.server import session_state


class PaidModelNotAllowed(RuntimeError):
    """The owner may not run the persona's paid model and no free one is set up."""


@dataclass(frozen=True)
class ModelChoice:
    """The model a turn runs, and the one its persona asked for."""

    model: str
    requested: str

    @property
    def substituted(self) -> bool:
        """True when a free model stands in for a paid one."""
        return self.model != self.requested


def is_free_id(model: str) -> bool:
    """True for an OpenRouter free model id (``…:free``)."""
    return model.endswith(":free")


def free_mode(config: dict[str, Any]) -> bool:
    """Whether the hub is in free mode (``llm.free_only``)."""
    return config_bool((config.get("llm") or {}).get("free_only"), False, key="llm.free_only")


def free_substitute(config: dict[str, Any]) -> str | None:
    """The free model a paid persona runs on for someone limited to free ones."""
    cfg = (config.get("llm") or {}).get("openai") or {}
    candidates = [*model_list(cfg.get("fallback_models")), str(cfg.get("model") or "")]
    return next((m for m in candidates if is_free_id(m)), None)


def operator_may_use_paid(operator: Any) -> bool:
    """Admins always may; anyone else only when an admin has allowed it."""
    if operator is None or not operator.enabled:
        return False
    return bool(operator.role == OperatorRole.ADMIN.value or operator.paid_models)


async def owner_may_use_paid(
    store: RegistryStore, config: dict[str, Any], agent: Agent | None
) -> bool:
    """Whether an agent's turns may run paid models: its owner's allowance.

    Outside free mode everyone may. In free mode an agent with no verified
    owner follows the hub default, which is free.
    """
    if not free_mode(config):
        return True
    subject = agent.owner_subject if agent is not None else None
    if not subject:
        return False
    return operator_may_use_paid(await store.get_dashboard_operator(subject))


def _applies(config: dict[str, Any], provider: str) -> bool:
    """Free/paid only means something for OpenRouter's catalogue."""
    if (provider or "openai") != "openai":
        return False
    base_url = str(((config.get("llm") or {}).get("openai") or {}).get("base_url") or "")
    return "openrouter.ai" in base_url


async def choose_model(
    store: RegistryStore, config: dict[str, Any], persona: Persona, device_id: str
) -> ModelChoice:
    """The model this agent's turn runs, honouring its owner's allowance.

    Raises:
        PaidModelNotAllowed: The persona's model is paid, the owner is limited
            to free models, and no free substitute is configured.
    """
    requested = resolved_model(config, persona.llm_provider, persona.llm_model or None)
    if is_free_id(requested) or not _applies(config, persona.llm_provider):
        session_state.set_model_notice(device_id, "")
        return ModelChoice(requested, requested)
    if await owner_may_use_paid(store, config, await store.get_agent(device_id)):
        session_state.set_model_notice(device_id, "")
        return ModelChoice(requested, requested)
    substitute = free_substitute(config)
    if substitute is None:
        raise PaidModelNotAllowed(
            f"This agent's owner can use free models only, and {requested} is paid. "
            "Pick a free model for this persona, or ask an admin to allow paid models."
        )
    session_state.set_model_notice(
        device_id,
        f"Running {substitute} instead of {requested}: this agent's owner can use "
        "free models only.",
    )
    return ModelChoice(substitute, requested)


async def turn_llm(
    store: RegistryStore, config: dict[str, Any], persona: Persona, device_id: str
) -> tuple[LLMProvider, ModelChoice]:
    """The provider for one of this agent's turns, and the model choice behind it."""
    choice = await choose_model(store, config, persona, device_id)
    return get_provider(persona.llm_provider, config, model_override=choice.model), choice
