"""Named credential profiles: separate agent credential dirs per environment."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

from vibepod.core.config import get_config_root

DEFAULT_PROFILE = "default"
PROFILE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def validate_profile_name(name: str) -> None:
    if not PROFILE_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            f"Invalid profile name '{name}': use lowercase letters, digits, '-' and '_', "
            "starting with a lowercase letter or digit."
        )


def profiles_root() -> Path:
    return get_config_root() / "profiles"


def profile_agents_root(profile: str) -> Path:
    """Return the directory holding per-agent credential dirs for a profile."""
    if profile == DEFAULT_PROFILE:
        return get_config_root() / "agents"
    return profiles_root() / profile / "agents"


def list_profiles() -> list[str]:
    names = [DEFAULT_PROFILE]
    root = profiles_root()
    if root.is_dir():
        names.extend(sorted(entry.name for entry in root.iterdir() if entry.is_dir()))
    return names


def profile_exists(profile: str) -> bool:
    if profile == DEFAULT_PROFILE:
        return True
    return (profiles_root() / profile).is_dir()


def create_profile(name: str) -> Path:
    validate_profile_name(name)
    if name == DEFAULT_PROFILE:
        raise ValueError("Profile 'default' always exists; no need to create it.")
    if profile_exists(name):
        raise ValueError(f"Profile '{name}' already exists.")
    path = profile_agents_root(name)
    path.mkdir(parents=True)
    return path


def remove_profile(name: str) -> None:
    if name == DEFAULT_PROFILE:
        raise ValueError("Profile 'default' cannot be removed.")
    validate_profile_name(name)
    if not profile_exists(name):
        raise ValueError(f"Profile '{name}' does not exist.")
    shutil.rmtree(profiles_root() / name)


def resolve_profile(cli_value: str | None, config: dict[str, Any]) -> str:
    """Resolve the active profile: CLI flag > VP_PROFILE > config key > default."""
    configured = config.get("profile")
    selected = (
        cli_value
        or os.environ.get("VP_PROFILE")
        or (configured if isinstance(configured, str) and configured else None)
        or DEFAULT_PROFILE
    )
    if selected != DEFAULT_PROFILE:
        validate_profile_name(selected)
    if not profile_exists(selected):
        raise ValueError(
            f"Profile '{selected}' does not exist. Create it with: vp profile create {selected}"
        )
    return selected
