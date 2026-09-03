"""The role a first-seen Cloudflare Access identity is given.

The Access policy is the guest list. This setting decides what being on it
means: read-only by default, or a working dashboard at an event where
everyone admitted is a builder.
"""

from __future__ import annotations

import pytest

from agent_hub.dashboard.authorization import DashboardAuthorization
from agent_hub.registry.models import OperatorRole
from agent_hub.registry.store import RegistryStore

_ACCESS = {
    "dashboard_access_team_domain": "example.cloudflareaccess.com",
    "dashboard_access_audience": "aud-123",
    "dashboard_admin_emails": "boss@example.com",
}


def _auth(store: RegistryStore, **server: str) -> DashboardAuthorization:
    return DashboardAuthorization(store, {"server": {**_ACCESS, **server}})


async def test_default_is_viewer(store: RegistryStore) -> None:
    auth = _auth(store)
    assert auth._default_role == OperatorRole.VIEWER.value


async def test_operator_can_be_configured(store: RegistryStore) -> None:
    auth = _auth(store, dashboard_default_role="operator")
    assert auth._default_role == OperatorRole.OPERATOR.value


async def test_case_and_whitespace_are_forgiven(store: RegistryStore) -> None:
    auth = _auth(store, dashboard_default_role="  Operator ")
    assert auth._default_role == OperatorRole.OPERATOR.value


async def test_admin_as_a_default_is_refused(store: RegistryStore) -> None:
    """A mistake in an Access policy must not hand out administration."""
    auth = _auth(store, dashboard_default_role="admin")
    assert auth._default_role == OperatorRole.VIEWER.value


async def test_an_unknown_role_falls_back_to_viewer(store: RegistryStore) -> None:
    auth = _auth(store, dashboard_default_role="superuser")
    assert auth._default_role == OperatorRole.VIEWER.value


async def test_new_identities_get_the_configured_role(store: RegistryStore) -> None:
    operator = await store.get_or_create_dashboard_operator(
        "sub-new", "builder@example.com", set(), OperatorRole.OPERATOR.value
    )
    assert operator.role == OperatorRole.OPERATOR.value


async def test_bootstrap_admins_still_win_over_the_default(store: RegistryStore) -> None:
    operator = await store.get_or_create_dashboard_operator(
        "sub-boss", "boss@example.com", {"boss@example.com"}, OperatorRole.OPERATOR.value
    )
    assert operator.role == OperatorRole.ADMIN.value


async def test_the_default_does_not_re_role_an_existing_operator(store: RegistryStore) -> None:
    """Someone demoted by hand stays demoted, whatever the default becomes."""
    await store.get_or_create_dashboard_operator(
        "sub-x", "x@example.com", set(), OperatorRole.VIEWER.value
    )
    await store.update_dashboard_operator("sub-x", OperatorRole.VIEWER, enabled=True)
    again = await store.get_or_create_dashboard_operator(
        "sub-x", "x@example.com", set(), OperatorRole.OPERATOR.value
    )
    assert again.role == OperatorRole.VIEWER.value


async def test_omitting_the_default_keeps_the_old_behaviour(store: RegistryStore) -> None:
    operator = await store.get_or_create_dashboard_operator("sub-y", "y@example.com", set())
    assert operator.role == OperatorRole.VIEWER.value


@pytest.mark.parametrize(
    ("env_key", "section", "leaf", "value"),
    [
        ("AGENT_HUB_LLM_FREE_ONLY", "llm", "free_only", "true"),
        ("AGENT_HUB_LLM_DEFAULT_PROVIDER", "llm", "default_provider", "openai"),
        ("AGENT_HUB_LLM_VISION_MODEL", "llm", "vision_model", "some/model"),
        ("AGENT_HUB_ASR_DEFAULT_PROVIDER", "asr", "default_provider", "moonshine"),
        ("AGENT_HUB_TTS_DEFAULT_PROVIDER", "tts", "default_provider", "edge"),
    ],
)
def test_underscored_leaves_in_provider_sections_resolve(
    monkeypatch: pytest.MonkeyPatch, env_key: str, section: str, leaf: str, value: str
) -> None:
    """These used to land one level too deep and be silently ignored.

    AGENT_HUB_LLM_FREE_ONLY became llm.free.only, so free mode could not be
    turned on by environment at all, and the compose files' ASR/TTS provider
    overrides never applied.
    """
    from agent_hub.config import _apply_env_overrides

    monkeypatch.setenv(env_key, value)
    config = _apply_env_overrides({})
    assert config[section][leaf] == value


def test_three_level_provider_keys_still_work(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_hub.config import _apply_env_overrides

    monkeypatch.setenv("AGENT_HUB_LLM_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("AGENT_HUB_ASR_MOONSHINE_MODEL_ARCH", "tiny")
    config = _apply_env_overrides({})
    assert config["llm"]["openai"]["api_key"] == "sk-test"
    assert config["asr"]["moonshine"]["model_arch"] == "tiny"


def test_server_dashboard_default_role_comes_through_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.config import Settings, _apply_env_overrides

    monkeypatch.setenv("AGENT_HUB_SERVER_DASHBOARD_DEFAULT_ROLE", "operator")
    settings = Settings.from_dict(_apply_env_overrides({}))
    assert settings.server.dashboard_default_role == "operator"
