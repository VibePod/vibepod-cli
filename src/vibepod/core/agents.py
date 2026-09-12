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
    # acp_command replaces `command` entirely for `vp run --acp` (Zed Agent
    # Panel via the Agent Client Protocol). None means the agent does not ship
    # an ACP adapter and `--acp` aborts with the list of supported agents.
    acp_command: list[str] | None = None
    # write_roots_env names an env var holding the ":"-joined directory
    # prefixes the agent is allowed to write to (hermes sandboxes its file
    # tools with HERMES_WRITE_SAFE_ROOT). The separator is the container's,
    # always ":", regardless of the host platform. When set, `--acp` appends
    # the host workspace path, which editors send as an absolute path.
    write_roots_env: str | None = None


AGENT_SPECS: dict[str, AgentSpec] = {
    "claude": AgentSpec(
        "claude",
        "anthropic",
        DEFAULT_IMAGES["claude"],
        "claude",
        ["claude"],
        "/claude",
        # CLAUDE_CODE_EXECUTABLE: the ACP adapter drives the image's Claude
        # Code instead of the copy bundled with its agent SDK (same reason as
        # CODEX_PATH below, and it keeps the version the image pins).
        {"CLAUDE_CONFIG_DIR": "/claude", "CLAUDE_CODE_EXECUTABLE": "/usr/local/bin/claude"},
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
        acp_command=["npx", "-y", "@agentclientprotocol/claude-agent-acp"],
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
        # Same launcher as `command`: --acp replaces it wholesale, so the
        # shebang/HOME workaround above has to be repeated here. Keep
        # --experimental-acp (the `--acp` alias only exists in gemini-cli
        # >= 0.33; the image tracks upstream and is not pinned).
        acp_command=[
            "env",
            "HOME=/config",
            "node",
            "/usr/local/bin/gemini",
            "--experimental-acp",
        ],
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
        acp_command=["opencode", "acp"],
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
        # Separate console script shipped by the same mistral-vibe package,
        # not a flag on `devstral`.
        acp_command=["vibe-acp"],
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
        acp_command=["auggie", "--acp"],
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
        # --stdio is the default transport, but it is mutually exclusive with
        # --port: pass it so an inherited config cannot move the adapter onto
        # a socket the attach stream never sees.
        acp_command=["copilot", "--acp", "--stdio"],
    ),
    "codex": AgentSpec(
        "codex",
        "openai",
        DEFAULT_IMAGES["codex"],
        "codex",
        ["codex"],
        "/config",
        # CODEX_PATH: the ACP adapter runs the image's codex instead of the
        # @openai/codex copy it bundles, whose optional platform package is
        # not always installed by npx (a missing one aborts the launch).
        {"HOME": "/config", "CODEX_PATH": "/usr/local/bin/codex"},
        ikwid_args=["--dangerously-bypass-approvals-and-sandbox"],
        llm_env_map={
            "base_url": "CODEX_OSS_BASE_URL",
        },
        llm_model_args=["--oss", "-m"],
        headless_prefix=["exec"],
        acp_command=["npx", "-y", "@agentclientprotocol/codex-acp"],
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
        # Community adapter (svkozak/pi-acp) that runs the image's `pi`. npx
        # uses a pre-installed copy when the image ships one and fetches it
        # otherwise; the fetch is cached in the config mount (HOME=/config).
        acp_command=["npx", "-y", "pi-acp"],
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
        acp_command=["jcode", "acp"],
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
        acp_command=["qwen", "--experimental-acp"],
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
    "hermes": AgentSpec(
        "hermes",
        "nousresearch",
        DEFAULT_IMAGES["hermes"],
        "hermes",
        ["hermes"],
        # The image is built on the official nousresearch/hermes-agent image,
        # whose state volume is /opt/data — config.yaml, .env, credentials,
        # sessions, skills and memories all live there, and the base image
        # bakes both HOME and HERMES_HOME to it. VibePod therefore mounts the
        # agent config directory at /opt/data and sets neither variable.
        "/opt/data",
        # Host-UID mapping goes through USER_UID/USER_GID, which VibePod
        # already exports and the image's 00-vibepod-uid cont-init hook
        # forwards as HERMES_UID/HERMES_GID. Do not set run_as_host_user:
        # the base image rejects `docker run --user <uid>`.
        #
        # HERMES_WRITE_SAFE_ROOT sandboxes Hermes' write_file/patch tools to a
        # set of directory prefixes. The base image bakes it to /opt/data
        # alone, which makes the project mount read-only to the agent, so the
        # workspace is appended here (":"-joined — the container's pathsep).
        {"HERMES_WRITE_SAFE_ROOT": "/opt/data:/workspace"},
        ikwid_args=["--yolo"],
        # Global LLM wiring is rejected by validate_llm_support: the pinned
        # runtime prioritizes saved providers and ACP has no routing flags.
        # `-z/--oneshot` takes the prompt as its value, and task.py emits
        # base_command + ikwid_prefix + headless_prefix + [prompt], which yields
        # `hermes --yolo -z "<prompt>"` — the prompt lands in -z's value slot and
        # --yolo is never swallowed by it.
        headless_prefix=["-z"],
        # `hermes-acp` is a separate console script from the same wheel (like
        # devstral's `vibe-acp`), not a flag on `hermes`, so it does not extend
        # spec.command. The image installs the package's [acp] extra, which
        # provides the `acp` module the adapter imports at startup.
        acp_command=["hermes-acp"],
        write_roots_env="HERMES_WRITE_SAFE_ROOT",
        # Hermes is pre-1.0 and its PyPI release line trails upstream main.
        preview=True,
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


def validate_llm_support(agent: str, config: dict[str, Any]) -> None:
    """Reject known-incompatible wiring rather than silently misroute requests."""
    if agent == "hermes" and config.get("llm", {}).get("enabled"):
        raise ValueError(
            "Hermes does not support VibePod's global LLM wiring in the pinned image. "
            "Set llm.enabled to false in your VibePod config and use Hermes-native "
            "provider/model setup (hermes setup inside the container). "
            "This applies to interactive, task, and ACP modes.",
        )


def validate_rootless_runtime(agent: str, rootless: bool) -> None:
    """Reject Hermes on rootless Podman before it maps the container to a UID it rejects.

    Rootless Podman launches the container with ``userns_mode=keep-id``, running as
    the invoking user's UID, and VibePod overwrites USER_UID/USER_GID with 0 for the
    entrypoint hooks. The pinned Hermes image needs its own bootstrap/runtime user:
    its ``main-wrapper`` exits 1 on an arbitrary non-hermes UID, and its UID-mapping
    hook ignores 0. Launching Hermes there would fail after the container starts, so
    reject before provisioning any network, proxy, or image.
    """
    if agent == "hermes" and rootless:
        raise ValueError(
            "Hermes does not support rootless Podman: the pinned image requires its "
            "own runtime user and rejects the arbitrary UID that rootless keep-id "
            "maps the container to (it also ignores a UID of 0). "
            "Run Hermes on rootful Docker/Podman instead.",
        )


def effective_agent_image(agent: str, config: dict[str, Any]) -> str:
    spec = get_agent_spec(agent)
    return str(config.get("agents", {}).get(agent, {}).get("image", spec.image))


def agent_config_dir(agent: str, profile: str = DEFAULT_PROFILE) -> Path:
    spec = get_agent_spec(agent)
    return profile_agents_root(profile) / spec.config_subdir
