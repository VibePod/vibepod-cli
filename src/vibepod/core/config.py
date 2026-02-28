"""Configuration loading and merging."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from vibepod.constants import (
    CONFIG_DIR,
    DEFAULT_ALIASES,
    DEFAULT_IMAGES,
    PROJECT_CONFIG_FILE,
)


def get_config_root() -> Path:
    """Return effective config directory, honoring VP_CONFIG_DIR."""
    custom = os.environ.get("VP_CONFIG_DIR")
    if custom:
        return Path(custom).expanduser().resolve()
    return Path(CONFIG_DIR)


def _default_config() -> dict[str, Any]:
    config_root = get_config_root()
    return {
        "version": 1,
        "default_agent": "claude",
        "auto_pull": False,
        "auto_remove": True,
        "network": "vibepod-network",
        "log_level": "info",
        "no_color": False,
        "agents": {
            "claude": {
                "enabled": True,
                "image": DEFAULT_IMAGES["claude"],
                "env": {},
                "volumes": [],
            },
            "gemini": {
                "enabled": True,
                "image": DEFAULT_IMAGES["gemini"],
                "env": {},
                "volumes": [],
            },
            "opencode": {
                "enabled": True,
                "image": DEFAULT_IMAGES["opencode"],
                "env": {},
                "volumes": [],
            },
            "devstral": {
                "enabled": True,
                "image": DEFAULT_IMAGES["devstral"],
                "env": {},
                "volumes": [],
            },
            "auggie": {
                "enabled": True,
                "image": DEFAULT_IMAGES["auggie"],
                "env": {},
                "volumes": [],
            },
            "copilot": {
                "enabled": True,
                "image": DEFAULT_IMAGES["copilot"],
                "env": {},
                "volumes": [],
            },
            "codex": {"enabled": True, "image": DEFAULT_IMAGES["codex"], "env": {}, "volumes": []},
        },
        "logging": {
            "enabled": True,
            "image": DEFAULT_IMAGES["datasette"],
            "db_path": str(config_root / "logs.db"),
            "ui_port": 8001,
        },
        "proxy": {
            "enabled": True,
            "image": DEFAULT_IMAGES["proxy"],
            "db_path": str(config_root / "proxy" / "proxy.db"),
            "ca_dir": str(config_root / "proxy" / "mitmproxy"),
            "ca_path": str(config_root / "proxy" / "mitmproxy" / "mitmproxy-ca-cert.pem"),
        },
        "aliases": DEFAULT_ALIASES.copy(),
    }


def ensure_config_dirs() -> None:
    """Ensure expected config directories exist."""
    config_root = get_config_root()
    config_root.mkdir(parents=True, exist_ok=True)
    (config_root / "agents").mkdir(parents=True, exist_ok=True)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    content = path.read_text(encoding="utf-8")
    if not content.strip():
        return {}
    loaded = yaml.safe_load(content)
    return loaded if isinstance(loaded, dict) else {}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge dictionaries into a new dictionary."""
    merged: dict[str, Any] = base.copy()
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(value, dict) and isinstance(existing, dict):
            merged[key] = deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _apply_env(config: dict[str, Any]) -> dict[str, Any]:
    mappings: dict[str, tuple[str, Any]] = {
        "VP_DEFAULT_AGENT": ("default_agent", str),
        "VP_AUTO_PULL": ("auto_pull", lambda x: x.lower() == "true"),
        "VP_LOG_LEVEL": ("log_level", str),
        "VP_NO_COLOR": ("no_color", lambda x: x.lower() == "true"),
        "VP_DATASETTE_PORT": ("logging.ui_port", int),
        "VP_PROXY_ENABLED": ("proxy.enabled", lambda x: x.lower() == "true"),
    }

    for env_key, (config_path, converter) in mappings.items():
        raw = os.environ.get(env_key)
        if raw is None:
            continue
        keys = config_path.split(".")
        target: dict[str, Any] = config
        for part in keys[:-1]:
            if part not in target or not isinstance(target[part], dict):
                target[part] = {}
            target = target[part]
        target[keys[-1]] = converter(raw)

    return config


def get_global_config_path() -> Path:
    return get_config_root() / "config.yaml"


def get_project_config_path(cwd: Path | None = None) -> Path:
    base = cwd or Path.cwd()
    return base / PROJECT_CONFIG_FILE


def get_config() -> dict[str, Any]:
    """Return merged effective config."""
    ensure_config_dirs()
    config = _default_config()
    config = deep_merge(config, _load_yaml(get_global_config_path()))

    project_path = get_project_config_path()
    if project_path.exists():
        config = deep_merge(config, _load_yaml(project_path))

    config = _apply_env(config)
    return config


def get_config_value(key: str, default: Any = None) -> Any:
    """Read a config value by dot notation."""
    value: Any = get_config()
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value
