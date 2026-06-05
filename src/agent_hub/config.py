"""Configuration loading and settings classes."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from loguru import logger

_ENV_PREFIX = "AGENT_HUB_"


def _apply_env_overrides(config: dict[str, Any]) -> dict[str, Any]:
    """Apply AGENT_HUB_<SECTION>_<KEY> environment variables onto config dict.

    Server and registry keys are flat, so AGENT_HUB_SERVER_WS_PORT maps to
    server.ws_port. Provider sections remain three-level, so
    AGENT_HUB_LLM_OPENAI_API_KEY maps to llm.openai.api_key.
    """
    for env_key, value in os.environ.items():
        if not env_key.startswith(_ENV_PREFIX):
            continue
        parts = env_key[len(_ENV_PREFIX) :].lower().split("_")
        if len(parts) < 2:
            continue
        if parts[0] in {"server", "registry"}:
            config.setdefault(parts[0], {})["_".join(parts[1:])] = value
            continue
        if len(parts) == 2:
            config.setdefault(parts[0], {})[parts[1]] = value
            continue
        parts = [parts[0], parts[1], "_".join(parts[2:])]
        target: dict[str, Any] = config
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value
    return config


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    ws_port: int = 8000
    http_port: int = 8003
    dashboard_port: int = 8001
    websocket: str = ""
    timezone_offset: int = -8


@dataclass
class RegistryConfig:
    db_path: str = "data/registry.db"


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
