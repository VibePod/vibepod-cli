"""Agent metadata and adapter-like helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vibepod.constants import DEFAULT_IMAGES, SUPPORTED_AGENTS
from vibepod.core.config import get_config_root


@dataclass(frozen=True)
class AgentSpec:
    id: str
    provider: str
    image: str
    config_subdir: str
    command: list[str]
    config_mount_path: str
    extra_env: dict[str, str]


AGENT_SPECS: dict[str, AgentSpec] = {
    "claude": AgentSpec(
        "claude",
        "anthropic",
        DEFAULT_IMAGES["claude"],
        "claude",
        ["claude"],
        "/claude",
        {"CLAUDE_CONFIG_DIR": "/claude"},
    ),
    "gemini": AgentSpec(
        "gemini",
        "google",
        DEFAULT_IMAGES["gemini"],
        "gemini",
        ["gemini"],
        "/config",
        {"HOME": "/config"},
    ),
    "opencode": AgentSpec(
        "opencode",
        "openai",
        DEFAULT_IMAGES["opencode"],
        "opencode",
        ["opencode"],
        "/config",
        {"HOME": "/config", "OPENCODE_CONFIG_DIR": "/config"},
    ),
    "devstral": AgentSpec(
        "devstral",
        "mistral",
        DEFAULT_IMAGES["devstral"],
        "devstral",
        ["devstral"],
        "/config",
        {"HOME": "/config"},
    ),
    "auggie": AgentSpec(
        "auggie",
        "augment",
        DEFAULT_IMAGES["auggie"],
        "auggie",
        ["auggie"],
        "/config",
        {"HOME": "/config"},
    ),
}


def is_supported_agent(agent: str) -> bool:
    return agent in SUPPORTED_AGENTS


def get_agent_spec(agent: str) -> AgentSpec:
    if agent not in AGENT_SPECS:
        raise ValueError(f"Unsupported agent: {agent}")
    return AGENT_SPECS[agent]


def effective_agent_image(agent: str, config: dict[str, Any]) -> str:
    spec = get_agent_spec(agent)
    return str(config.get("agents", {}).get(agent, {}).get("image", spec.image))


def agent_config_dir(agent: str) -> Path:
    spec = get_agent_spec(agent)
    return get_config_root() / "agents" / spec.config_subdir
