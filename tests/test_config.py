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
    monkeypatch.setenv("AGENT_HUB_SERVER_HEARTBEAT_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("AGENT_HUB_SERVER_HEARTBEAT_TIMEOUT_SECONDS", "180")

    settings = Settings.from_dict(load_config(config_path))

    assert settings.server.ws_port == 9000
    assert settings.server.http_port == 9003
    assert settings.server.dashboard_port == 9001
    assert settings.server.timezone_offset == -5
    assert settings.server.heartbeat_interval_seconds == 60
    assert settings.server.heartbeat_timeout_seconds == 180


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
    monkeypatch.setenv(
        "AGENT_HUB_SERVER_DASHBOARD_ACCESS_TEAM_DOMAIN",
        "team.cloudflareaccess.com",
    )
    monkeypatch.setenv("AGENT_HUB_SERVER_DASHBOARD_ACCESS_AUDIENCE", "app-audience")
    monkeypatch.setenv(
        "AGENT_HUB_SERVER_DASHBOARD_ADMIN_EMAILS", "admin@example.com,ops@example.com"
    )
    monkeypatch.setenv("AGENT_HUB_SERVER_DASHBOARD_IMAGE_ROOT", "/srv/images")
    monkeypatch.setenv("AGENT_HUB_SERVER_IMAGE_TOKEN", "img-secret")

    config = load_config(config_path)

    assert config["server"]["enrollment_token"] == "enroll-secret"
    assert config["server"]["dashboard_password"] == "hunter2"
    assert config["server"]["dashboard_access_team_domain"] == "team.cloudflareaccess.com"
    assert config["server"]["dashboard_access_audience"] == "app-audience"
    assert config["server"]["dashboard_admin_emails"] == ("admin@example.com,ops@example.com")
    assert config["server"]["dashboard_image_root"] == "/srv/images"
    assert config["server"]["image_token"] == "img-secret"
    assert "dashboard" not in config["server"]
    assert "image" not in config["server"]

    settings = Settings.from_dict(config)
    assert settings.server.enrollment_token == "enroll-secret"
    assert settings.server.dashboard_password == "hunter2"
    assert settings.server.dashboard_access_team_domain == "team.cloudflareaccess.com"
    assert settings.server.dashboard_access_audience == "app-audience"
    assert settings.server.dashboard_admin_emails == "admin@example.com,ops@example.com"


def test_resolve_timezone_prefers_iana_name_over_offset():
    from datetime import datetime

    from agent_hub.config import resolve_timezone

    tz = resolve_timezone("America/New_York", -8)
    # DST-aware: EDT in July, EST in January.
    assert datetime(2026, 7, 1, tzinfo=tz).utcoffset().total_seconds() == -4 * 3600
    assert datetime(2026, 1, 1, tzinfo=tz).utcoffset().total_seconds() == -5 * 3600


def test_resolve_timezone_falls_back_to_offset_for_blank_or_unknown_name():
    from datetime import datetime

    from agent_hub.config import resolve_timezone

    for name in ("", "   ", "Not/AZone"):
        tz = resolve_timezone(name, -5)
        assert datetime(2026, 6, 1, tzinfo=tz).utcoffset().total_seconds() == -5 * 3600


def test_to_local_treats_naive_input_as_utc():
    from datetime import datetime

    from agent_hub.config import resolve_timezone, to_local

    ny = resolve_timezone("America/New_York")
    local = to_local(datetime(2026, 9, 1, 23, 48, 0), ny)  # 23:48 UTC
    assert (local.hour, local.minute) == (19, 48)  # EDT = UTC-4
    assert local.tzinfo is ny
