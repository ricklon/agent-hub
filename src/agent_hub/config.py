"""Configuration loading and settings classes."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from loguru import logger

_ENV_PREFIX = "AGENT_HUB_"


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    ws_port: int = 8000
    http_port: int = 8003
    dashboard_port: int = 8001
    websocket: str = ""
    timezone_offset: int = -8
    timezone: str = ""


@dataclass
class RegistryConfig:
    db_path: str = "data/registry.db"


_TOP_LEVEL_CONFIGS = {
    "server": ServerConfig,
    "registry": RegistryConfig,
}


def _section_leaf_keys(section: str) -> set[str]:
    config_cls = _TOP_LEVEL_CONFIGS.get(section)
    if config_cls is None or not is_dataclass(config_cls):
        return set()
    return {field.name for field in fields(config_cls)}


def _apply_env_overrides(config: dict[str, Any]) -> dict[str, Any]:
    """Apply AGENT_HUB_<SECTION>_<KEY> environment variables onto config dict.

    Known top-level sections resolve against their dataclass field names so
    underscored leaves survive intact: AGENT_HUB_SERVER_WS_PORT → server.ws_port.
    Provider-style sections remain three-level:
    AGENT_HUB_LLM_OPENAI_API_KEY → llm.openai.api_key.
    """
    for env_key, value in os.environ.items():
        if not env_key.startswith(_ENV_PREFIX):
            continue
        tokens = env_key[len(_ENV_PREFIX) :].lower().split("_")
        if len(tokens) < 2:
            continue

        section = tokens[0]
        tail = tokens[1:]
        section_keys = _section_leaf_keys(section)
        flat_key = "_".join(tail)

        if flat_key in section_keys:
            parts = [section, flat_key]
        elif len(tail) >= 2:
            parts = [section, tail[0], "_".join(tail[1:])]
        else:
            parts = [section, tail[0]]

        target: dict[str, Any] = config
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value
    return config


@dataclass
class Settings:
    """Top-level application settings derived from .config.yaml + env overrides."""

    server: ServerConfig = field(default_factory=ServerConfig)
    registry: RegistryConfig = field(default_factory=RegistryConfig)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, config: dict[str, Any]) -> Settings:
        """Build Settings from a config dict (already env-overridden).

        Args:
            config: Raw config dict, typically from load_config().

        Returns:
            Populated Settings instance.
        """
        srv = config.get("server", {})
        reg = config.get("registry", {})
        return cls(
            server=ServerConfig(
                host=str(srv.get("host", "0.0.0.0")),
                ws_port=int(srv.get("ws_port", 8000)),
                http_port=int(srv.get("http_port", 8003)),
                dashboard_port=int(srv.get("dashboard_port", 8001)),
                websocket=str(srv.get("websocket", "")),
                timezone_offset=int(srv.get("timezone_offset", -8)),
                timezone=str(srv.get("timezone", "")),
            ),
            registry=RegistryConfig(
                db_path=str(reg.get("db_path", "data/registry.db")),
            ),
            raw=config,
        )


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load YAML config and apply environment variable overrides.

    Args:
        path: Path to .config.yaml. Defaults to data/.config.yaml.

    Returns:
        Config dict ready to pass to Settings.from_dict().
    """
    load_dotenv()  # loads .env if present, no-op otherwise

    if path is None:
        path = Path("data/.config.yaml")

    if not path.exists():
        logger.warning(f"Config file {path} not found — using defaults and env vars")
        config: dict[str, Any] = {}
    else:
        with path.open() as f:
            config = yaml.safe_load(f) or {}

    return _apply_env_overrides(config)


def load_settings(path: Path | None = None) -> Settings:
    """Convenience: load config and return a Settings object.

    Args:
        path: Optional path to .config.yaml.

    Returns:
        Settings instance.
    """
    return Settings.from_dict(load_config(path))
