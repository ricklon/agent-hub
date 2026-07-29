"""Tests for configuration loading and environment overrides."""

from __future__ import annotations

from agent_hub.config import Settings, load_config


def test_env_overrides_server_fields_with_underscored_names(monkeypatch, tmp_path):
    config_path = tmp_path / ".config.yaml"
    config_path.write_text("server:\n  ws_port: 8000\n  timezone_offset: -8\n")
    monkeypatch.setenv("AGENT_HUB_SERVER_WS_PORT", "9000")
    monkeypatch.setenv("AGENT_HUB_SERVER_HTTP_PORT", "9003")
    monkeypatch.setenv("AGENT_HUB_SERVER_DASHBOARD_PORT", "9001")
    monkeypatch.setenv("AGENT_HUB_SERVER_TIMEZONE_OFFSET", "-5")

    settings = Settings.from_dict(load_config(config_path))

    assert settings.server.ws_port == 9000
    assert settings.server.http_port == 9003
    assert settings.server.dashboard_port == 9001
    assert settings.server.timezone_offset == -5


def test_env_overrides_nested_provider_leaf_with_underscore(monkeypatch, tmp_path):
    config_path = tmp_path / ".config.yaml"
    config_path.write_text("{}\n")
    monkeypatch.setenv("AGENT_HUB_LLM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AGENT_HUB_LLM_OPENAI_BASE_URL", "http://example.test/v1")

    config = load_config(config_path)

    assert config["llm"]["openai"]["api_key"] == "test-key"
    assert config["llm"]["openai"]["base_url"] == "http://example.test/v1"


def test_env_overrides_hosting_auth_keys_stay_flat(monkeypatch, tmp_path):
    """Auth keys are ServerConfig fields, so they must not nest under server.dashboard."""
    config_path = tmp_path / ".config.yaml"
    config_path.write_text("{}\n")
    monkeypatch.setenv("AGENT_HUB_SERVER_ENROLLMENT_TOKEN", "enroll-secret")
    monkeypatch.setenv("AGENT_HUB_SERVER_DASHBOARD_PASSWORD", "hunter2")
    monkeypatch.setenv("AGENT_HUB_SERVER_DASHBOARD_IMAGE_ROOT", "/srv/images")
    monkeypatch.setenv("AGENT_HUB_SERVER_IMAGE_TOKEN", "img-secret")

    config = load_config(config_path)

    assert config["server"]["enrollment_token"] == "enroll-secret"
    assert config["server"]["dashboard_password"] == "hunter2"
    assert config["server"]["dashboard_image_root"] == "/srv/images"
    assert config["server"]["image_token"] == "img-secret"
    assert "dashboard" not in config["server"]
    assert "image" not in config["server"]

    settings = Settings.from_dict(config)
    assert settings.server.enrollment_token == "enroll-secret"
    assert settings.server.dashboard_password == "hunter2"
