"""Agent metadata and adapter-like helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vibepod.constants import AGENT_ALIASES, AGENT_SHORTCUTS, DEFAULT_IMAGES, SUPPORTED_AGENTS
from vibepod.core.profiles import DEFAULT_PROFILE, profile_agents_root


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
    headless_prefix: list[str] | None = None
    # headless_command replaces `command` entirely for `vp task` when the
    # agent's one-shot invocation is not `command + headless_prefix` (dsh's
    # interactive command is `dsh web`, its one-shot is `dsh --profile headless`).
    # preview marks developer-preview agents; run/task print a warning.
    # web_container_port names the container port serving a Web UI so run can
    # print the published URL after start.
    headless_command: list[str] | None = None
    preview: bool = False
    web_container_port: int | None = None


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
        headless_prefix=["-p"],
    ),
    "gemini": AgentSpec(
        "gemini",
        "google",
        DEFAULT_IMAGES["gemini"],
        "gemini",
        # Run via node to bypass shebang parsing in Alpine BusyBox (/usr/bin/env has no -S),
        # and force HOME to the mounted config path expected by VibePod.
        ["env", "HOME=/config", "node", "/usr/local/bin/gemini"],
        "/config",
        {"HOME": "/config"},
        ikwid_args=["--approval-mode=yolo"],
    ),
    "opencode": AgentSpec(
        "opencode",
        "openai",
        DEFAULT_IMAGES["opencode"],
        "opencode",
        ["opencode"],
        "/config",
        {
            "HOME": "/config",
            "OPENCODE_CONFIG_DIR": "/config",
            "XDG_CONFIG_HOME": "/config/.config",
            "XDG_DATA_HOME": "/config/.local/share",
            "XDG_STATE_HOME": "/config/.local/state",
            "XDG_CACHE_HOME": "/config/.cache",
        },
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
        ikwid_args=["--auto-approve"],
    ),
    "auggie": AgentSpec(
        "auggie",
        "augment",
        DEFAULT_IMAGES["auggie"],
        "auggie",
        ["auggie"],
        "/config",
        {"HOME": "/config"},
        headless_prefix=["--print"],
    ),
    "copilot": AgentSpec(
        "copilot",
        "github",
        DEFAULT_IMAGES["copilot"],
        "copilot",
        ["copilot"],
        "/config",
        {"HOME": "/config"},
        ikwid_args=["--yolo"],
    ),
    "codex": AgentSpec(
        "codex",
        "openai",
        DEFAULT_IMAGES["codex"],
        "codex",
        ["codex"],
        "/config",
        {"HOME": "/config"},
        ikwid_args=["--dangerously-bypass-approvals-and-sandbox"],
        llm_env_map={
            "base_url": "CODEX_OSS_BASE_URL",
        },
        llm_model_args=["--oss", "-m"],
        headless_prefix=["exec"],
    ),
    "pi": AgentSpec(
        "pi",
        "earendil",
        DEFAULT_IMAGES["pi"],
        "pi",
        ["pi"],
        "/config",
        {"HOME": "/config", "PI_CODING_AGENT_DIR": "/config/.pi/agent"},
        ikwid_args=["--approve"],
    ),
    "agy": AgentSpec(
        "agy",
        "google",
        DEFAULT_IMAGES["agy"],
        "agy",
        ["agy"],
        "/home/agy",
        {"HOME": "/home/agy"},
        platform="linux/amd64",
        ikwid_args=["--dangerously-skip-permissions"],
    ),
    "tau": AgentSpec(
        "tau",
        "huggingface",
        DEFAULT_IMAGES["tau"],
        "tau",
        ["tau"],
        "/config",
        # Tau derives every user-level path from HOME (~/.tau for sessions,
        # credentials, providers.json and catalog.toml), so the persisted config
        # mount at /config is all it needs.
        {"HOME": "/config", "TAU_NO_UPDATE_CHECK": "1"},
        headless_prefix=["-p"],
    ),
    "jcode": AgentSpec(
        "jcode",
        "1jehuang",
        DEFAULT_IMAGES["jcode"],
        "jcode",
        ["jcode"],
        "/config",
        # jcode resolves ~/.jcode (sessions, auth, config.toml, mcp.json) via
        # $HOME and its provider env files via XDG config (~/.config/jcode),
        # so pointing HOME at the persisted /config mount covers both.
        {"HOME": "/config", "JCODE_NO_AUTO_UPDATE": "1"},
        headless_prefix=["run"],
    ),
    "freebuff": AgentSpec(
        "freebuff",
        "codebuffai",
        DEFAULT_IMAGES["freebuff"],
        "freebuff",
        ["freebuff"],
        "/freebuff",
        # The freebuff container entrypoint handles symlinking /freebuff
        # to the correct internal config paths (~/.config/manicode) and
        # overrides HOME internally. We just mount to /freebuff.
        {"FREEBUFF_CONFIG_DIR": "/freebuff"},
    ),
    "qwen": AgentSpec(
        "qwen",
        "qwenlm",
        DEFAULT_IMAGES["qwen"],
        "qwen",
        ["qwen"],
        "/qwen",
        # The qwen container entrypoint symlinks ~/.qwen to the QWEN_CONFIG_DIR
        # mount (/qwen) and overrides HOME internally. We just mount to /qwen.
        {"QWEN_CONFIG_DIR": "/qwen"},
        ikwid_args=["--approval-mode=yolo"],
        headless_prefix=["-p"],
    ),
    "dsh": AgentSpec(
        "dsh",
        "deepseek",
        DEFAULT_IMAGES["dsh"],
        "dsh",
        # dsh is Web-UI-first: `dsh web` serves http://127.0.0.1:3080 in-container
        # (it intentionally rejects --host 0.0.0.0), and the image entrypoint runs
        # a socat forwarder on VIBEPOD_WEB_FORWARD_PORT so Docker can publish it.
        # --no-open: the container has no browser; the user opens the printed URL.
        ["dsh", "web", "--no-open"],
        "/config",
        # dsh keeps all user data under $DSH_HOME (~/.dsh), so HOME on the
        # persisted mount is the whole persistence contract.
        # NODE_USE_ENV_PROXY: dsh calls DeepSeek via Node's global fetch, which
        # ignores HTTP(S)_PROXY unless this flag is set (Node >= 22.21) — without
        # it the traffic bypasses the vibepod-proxy mitm container.
        {"HOME": "/config", "VIBEPOD_WEB_FORWARD_PORT": "3081", "NODE_USE_ENV_PROXY": "1"},
        headless_command=["dsh", "--profile", "headless"],
        preview=True,
        web_container_port=3081,
    ),
}

_SHORTCUT_BY_AGENT = {agent: shortcut for shortcut, agent in AGENT_SHORTCUTS.items()}


def is_supported_agent(agent: str) -> bool:
    return agent in SUPPORTED_AGENTS


def resolve_agent_name(agent: str) -> str | None:
    normalized = agent.strip().lower()
    if normalized in SUPPORTED_AGENTS:
        return normalized
    return AGENT_SHORTCUTS.get(normalized) or AGENT_ALIASES.get(normalized)


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


def agent_config_dir(agent: str, profile: str = DEFAULT_PROFILE) -> Path:
    spec = get_agent_spec(agent)
    return profile_agents_root(profile) / spec.config_subdir
