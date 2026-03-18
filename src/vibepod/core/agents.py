"""Agent metadata and adapter-like helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vibepod.constants import AGENT_SHORTCUTS, DEFAULT_IMAGES, SUPPORTED_AGENTS
from vibepod.core.config import get_config_root


@dataclass(frozen=True)
class AgentSpec:
    id: str
    provider: str
    image: str
    config_subdir: str
    command: list[str] | None
    config_mount_path: str
    extra_env: dict[str, str]
    platform: str | None = None
    run_as_host_user: bool = False
    ikwid_args: list[str] | None = None
    llm_env_map: dict[str, str | list[str]] | None = None
    llm_model_args: list[str] | None = None


AGENT_SPECS: dict[str, AgentSpec] = {
    "claude": AgentSpec(
        "claude",
        "anthropic",
        DEFAULT_IMAGES["claude"],
        "claude",
        ["claude"],
        "/claude",
        {"CLAUDE_CONFIG_DIR": "/claude"},
        ikwid_args=["--dangerously-skip-permissions"],
        llm_env_map={
            "base_url": "ANTHROPIC_BASE_URL",
            "api_key": ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"],
            "model": [
                "ANTHROPIC_MODEL",
                "ANTHROPIC_DEFAULT_OPUS_MODEL",
                "ANTHROPIC_DEFAULT_SONNET_MODEL",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            ],
        },
        llm_model_args=["--model"],
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
        None,
        "/config",
        {"HOME": "/config", "WORKSPACE_PATH": "/workspace"},
        platform="linux/amd64",
        run_as_host_user=True,
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
    "copilot": AgentSpec(
        "copilot",
        "github",
        DEFAULT_IMAGES["copilot"],
        "copilot",
        ["copilot"],
        "/config",
        {"HOME": "/config"},
    ),
    "codex": AgentSpec(
        "codex",
        "openai",
        DEFAULT_IMAGES["codex"],
        "codex",
        ["codex"],
        "/config",
        {"HOME": "/config"},
        ikwid_args=["--full-auto"],
        llm_env_map={
            "base_url": "CODEX_OSS_BASE_URL",
        },
        llm_model_args=["--oss", "-m"],
    ),
}

_SHORTCUT_BY_AGENT = {agent: shortcut for shortcut, agent in AGENT_SHORTCUTS.items()}


def is_supported_agent(agent: str) -> bool:
    return agent in SUPPORTED_AGENTS


def resolve_agent_name(agent: str) -> str | None:
    normalized = agent.strip().lower()
    if normalized in SUPPORTED_AGENTS:
        return normalized
    return AGENT_SHORTCUTS.get(normalized)


def get_agent_shortcut(agent: str) -> str | None:
    normalized = agent.strip().lower()
    return _SHORTCUT_BY_AGENT.get(normalized)


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
