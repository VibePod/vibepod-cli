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
    # headless_options_before_prompt moves passthrough args ahead of the prompt
    # and ends option parsing with `--` (pi rejects a prompt that starts with "-").
    headless_options_before_prompt: bool = False
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
    # rootless_runtime_ids is the image's own "uid:gid" runtime user, for
    # images whose init must start as root and then drop to that user (s6
    # images reject any other non-root start). On rootless Podman the host
    # user is mapped onto it via keep-id:uid=,gid= and the container starts
    # as namespace root instead of as the host UID. None keeps plain keep-id.
    rootless_runtime_ids: str | None = None
    # rootless_runtime_id_env names the env vars through which the image's
    # init overrides its runtime (uid, gid), highest precedence first; an
    # empty value falls through to the next name, like `${A:-${B:-}}`. On
    # rootless Podman the effective value must equal rootless_runtime_ids, or
    # the init would drop to a UID the keep-id mapping does not cover.
    rootless_runtime_id_env: tuple[tuple[str, ...], tuple[str, ...]] = ((), ())


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
    "vibe": AgentSpec(
        "vibe",
        "mistral",
        DEFAULT_IMAGES["vibe"],
        "devstral",  # Preserve existing credentials and sessions on rename.
        None,
        "/config",
        {"HOME": "/config", "WORKSPACE_PATH": "/workspace"},
        platform="linux/amd64",
        run_as_host_user=True,
        ikwid_args=["--auto-approve"],
        # Separate console script shipped by the same mistral-vibe package,
        # not a flag on `vibe`.
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
        # JSON mode is non-interactive by itself and streams every event plus
        # the final answer as JSON lines, so task logs stay machine-readable.
        headless_prefix=["--mode", "json"],
        headless_options_before_prompt=True,
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
        # vibe's `vibe-acp`), not a flag on `hermes`, so it does not extend
        # spec.command. The image installs the package's [acp] extra, which
        # provides the `acp` module the adapter imports at startup.
        acp_command=["hermes-acp"],
        write_roots_env="HERMES_WRITE_SAFE_ROOT",
        # Upstream's Dockerfile creates the runtime user with
        # `useradd -u 10000 hermes`; main-wrapper and stage2-hook exit 1 for
        # any non-root start under a different UID.
        rootless_runtime_ids="10000:10000",
        # The image's init hooks keep an explicit HERMES_UID/HERMES_GID (or
        # the upstream PUID/PGID aliases) over the USER_UID/USER_GID bridge.
        rootless_runtime_id_env=(("HERMES_UID", "PUID"), ("HERMES_GID", "PGID")),
        # Hermes is pre-1.0 and its PyPI release line trails upstream main.
        preview=True,
    ),
    "cline": AgentSpec(
        "cline",
        "cline",
        DEFAULT_IMAGES["cline"],
        "cline",
        ["cline"],
        "/config",
        # Cline derives all user state from HOME: ~/.cline holds provider
        # credentials (data/settings/providers.json), sessions, rules, skills
        # and hooks. The image disables the CLI's background self-update
        # (CLINE_NO_AUTO_UPDATE=1) so it never reinstalls over the pinned copy.
        # No llm_env_map: the CLI selects its provider from saved settings or
        # -P/--provider, and VibePod has no verified env-only mapping for a
        # custom base URL (`cline auth --baseurl` persists it instead). The
        # compiled CLI honours HTTP(S)_PROXY and NODE_EXTRA_CA_CERTS natively.
        {"HOME": "/config"},
        # Tool auto-approval defaults to on but the TUI persists the user's
        # toggle; --auto-approve true overrides it for the run. --yolo is not
        # used: it switches to a restricted unsupervised mode that exits after
        # one turn.
        ikwid_args=["--auto-approve", "true"],
        # A positional prompt without -i/--tui is Cline's one-shot mode. Without
        # an explicit mode flag it reuses the plan/act mode last chosen in the
        # TUI, so --act keeps a task from silently running read-only in plan mode.
        headless_prefix=["--act"],
        # Built-in Agent Client Protocol server over stdio.
        acp_command=["cline", "--acp"],
    ),
    "junie": AgentSpec(
        "junie",
        "jetbrains",
        DEFAULT_IMAGES["junie"],
        "junie",
        ["junie"],
        "/config",
        # Junie resolves its home from $JUNIE_HOME, falling back to ~/.junie
        # (utils/JuniePaths.kt): settings, sessions, skills, MCP config and,
        # without a Secret Service, credentials in secure_credentials.json.
        # Setting it explicitly keeps state on the /config mount even when the
        # image entrypoint maps the host UID onto an existing passwd entry.
        # JUNIE_SKIP_UPDATE_CHECK: the image pins a release, so Junie must not
        # stage self-updates under ~/.local/share/junie.
        {"HOME": "/config", "JUNIE_HOME": "/config/.junie", "JUNIE_SKIP_UPDATE_CHECK": "true"},
        # Brave Mode auto-approves actions. Upstream documents it as
        # interactive-only; non-interactive runs accept and ignore it, so the
        # same flag is safe for `vp task`.
        ikwid_args=["--brave"],
        # No global LLM wiring: Junie only takes models from its built-in
        # list or custom model profiles, and validates --model before its
        # LiteLLM model discovery finishes, so arbitrary OpenAI-compatible
        # model names fail at startup. BYOK/LiteLLM is configured natively.
        # `--task` (instead of the positional task) keeps a prompt starting
        # with "-" from being parsed as an option; the task yields
        # `junie --brave --task "<prompt>"`.
        headless_prefix=["--task"],
        acp_command=["junie", "--acp=true"],
    ),
    "cursor": AgentSpec(
        "cursor",
        "anysphere",
        DEFAULT_IMAGES["cursor"],
        "cursor",
        ["cursor-agent"],
        "/config",
        # Cursor derives all user state from HOME: ~/.cursor (cli-config.json,
        # chats, skills) and, on Linux where it has no keychain store,
        # ~/.config/cursor/auth.json. The image entrypoint pins the release
        # channel to "static" in cli-config.json so the background self-update
        # never downloads packages onto the persisted mount.
        # No llm_env_map: the CLI only talks to Cursor's own API (--endpoint /
        # CURSOR_API_ENDPOINT), so there is no OpenAI-compatible base URL to route.
        # Enable Node's environment proxy support explicitly, rather than
        # relying on the image to supply it (as with dsh above).
        {"HOME": "/config", "NODE_USE_ENV_PROXY": "1"},
        ikwid_args=["--force"],
        # `-p/--print` is a boolean flag and the prompt stays positional. Print
        # mode exits with "Workspace Trust Required" on a directory it has not
        # seen unless --trust, --yolo or -f is passed, so --trust is part of the
        # one-shot prefix; the workspace is the directory the user launched from.
        # It is unconditional because a detached task cannot answer the trust
        # prompt, and it only trusts the workspace: command approvals are still
        # enforced unless --ikwid adds --force.
        headless_prefix=["--trust", "-p"],
        # Hidden `acp` subcommand on the same binary (Agent Client Protocol
        # server over stdio).
        acp_command=["cursor-agent", "acp"],
    ),
    "warp": AgentSpec(
        "warp",
        "warp",
        DEFAULT_IMAGES["warp"],
        "warp",
        ["warp"],
        "/config",
        # Warp derives all user state from HOME: ~/.config/warp-terminal
        # (settings), ~/.local/state/warp-terminal (conversation database) and
        # ~/.local/share/keyrings. The login lives in the Secret Service, which
        # the image provides with a private session bus and keyring so that it
        # persists on this mount.
        # WARP_TUI_DISABLE_AUTOUPDATE: the background self-update would stage
        # a full build under ~/.warp/tui on the persisted mount, while the
        # image keeps running its baked-in binary. Set here as well as in the
        # image so an image override cannot bring it back.
        # No llm_env_map: inference goes through Warp's own service, and its
        # bring-your-own-key support takes provider keys but no base URL.
        # HTTP(S)_PROXY and SSL_CERT_FILE are honoured natively.
        {"HOME": "/config", "WARP_TUI_DISABLE_AUTOUPDATE": "1"},
        ikwid_args=["--auto-approve"],
        # No headless_prefix and no acp_command: the CLI is interactive only.
        # It takes no prompt argument, has no print mode and ships no Agent
        # Client Protocol server.
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


def rootless_podman_identity(agent: str, env: dict[str, str]) -> tuple[str, str | None]:
    """Return ``(userns_mode, container_user)`` for a rootless Podman launch.

    Plain ``keep-id`` runs the container as the invoking user's UID, so files
    written to bind mounts stay owned by the host user. Images with a fixed
    runtime user (``rootless_runtime_ids``) instead map the host user onto that
    user and start as namespace root, so their init can bootstrap before
    dropping privileges to a UID that is the host user on the host side.

    Raises ``ValueError`` when ``env`` sets the image's effective runtime
    UID/GID to anything other than ``rootless_runtime_ids``: the init would
    then drop to an ID outside the keep-id mapping and lose access to the
    workspace and config mounts. The effective value is the first non-empty
    name in ``rootless_runtime_id_env``, as the image's init resolves it, so
    an empty override or an alias shadowed by a matching one is accepted.
    """
    spec = get_agent_spec(agent)
    ids = spec.rootless_runtime_ids
    if ids is None:
        return "keep-id", None
    uid, gid = ids.split(":")
    uid_names, gid_names = spec.rootless_runtime_id_env
    conflicts: list[str] = []
    for names, expected in ((uid_names, uid), (gid_names, gid)):
        name = next((n for n in names if env.get(n)), None)
        if name is not None and env[name] != expected:
            conflicts.append(f"{name}={env[name]}")
    if conflicts:
        raise ValueError(
            f"{agent} on rootless Podman must run as its image user {ids}; "
            f"remove the conflicting override(s): {', '.join(conflicts)}.",
        )
    return f"keep-id:uid={uid},gid={gid}", "0:0"


def effective_agent_image(agent: str, config: dict[str, Any]) -> str:
    spec = get_agent_spec(agent)
    return str(config.get("agents", {}).get(agent, {}).get("image", spec.image))


def agent_config_dir(agent: str, profile: str = DEFAULT_PROFILE) -> Path:
    spec = get_agent_spec(agent)
    return profile_agents_root(profile) / spec.config_subdir
