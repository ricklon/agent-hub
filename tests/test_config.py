"""Tests for configuration loading and environment overrides."""

from __future__ import annotations

from agent_hub.config import load_config


def test_server_env_keys_with_underscores_are_flat(monkeypatch, tmp_path) -> None:
    """SERVER_* env keys map to server.<snake_case_key>."""
    monkeypatch.setenv("AGENT_HUB_SERVER_WS_PORT", "9000")
    monkeypatch.setenv("AGENT_HUB_SERVER_ENROLLMENT_TOKEN", "enroll-secret")

    config = load_config(tmp_path / "missing.yaml")

    assert config["server"]["ws_port"] == "9000"
    assert config["server"]["enrollment_token"] == "enroll-secret"


def test_provider_env_keys_remain_three_level(monkeypatch, tmp_path) -> None:
    """Provider keys keep provider nesting and snake_case leaf names."""
    monkeypatch.setenv("AGENT_HUB_LLM_OPENAI_API_KEY", "sk-test")

    config = load_config(tmp_path / "missing.yaml")

    assert config["llm"]["openai"]["api_key"] == "sk-test"
