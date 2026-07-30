"""Helpers shared by `vp run` and `vp task create` for launching agent containers."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

from vibepod.core import overlay
from vibepod.core.config import load_project_config
from vibepod.core.docker import DockerClientError, DockerManager
from vibepod.utils.console import error, warning

CLAUDE_TOKEN_FILENAME = "oauth-token"


def claude_stored_token_path(config_dir: Path) -> Path:
    return config_dir / CLAUDE_TOKEN_FILENAME


def read_claude_stored_token(config_dir: Path) -> str | None:
    path = claude_stored_token_path(config_dir)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        warning(f"Could not read stored claude token at {path}: {exc}")
        return None
    return token or None


def write_claude_stored_token(config_dir: Path, token: str) -> Path:
    path = claude_stored_token_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        fchmod = getattr(os, "fchmod", None)
        if callable(fchmod):
            # fchmod overrides umask; os.open mode alone is umask-filtered
            fchmod(fd, 0o600)
    except OSError:
        warning(f"Could not restrict permissions on {path}; token may be readable by other users")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token.strip() + "\n")
    return path


def parse_env_pairs(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for entry in values:
        if "=" not in entry:
            raise typer.BadParameter(f"Invalid --env value '{entry}', expected KEY=VALUE")
        key, value = entry.split("=", 1)
        if not key:
            raise typer.BadParameter("Environment variable key cannot be empty")
        parsed[key] = value
    return parsed


def _overlay_setting(workspace_path: Path, agent: str, agent_cfg: dict[str, Any]) -> Any:
    """``agents.<agent>.overlay`` with the launched workspace's config winning.

    *agent_cfg* comes from get_config(), which merges the project file of the
    *current* directory; with ``-w`` pointing at another project, that
    project's own setting must decide whether its overlay applies.
    """
    agents = load_project_config(workspace_path).get("agents")
    entry = agents.get(agent) if isinstance(agents, dict) else None
    if isinstance(entry, dict) and "overlay" in entry:
        return entry["overlay"]
    return agent_cfg.get("overlay")


def apply_overlay_if_enabled(
    *,
    manager: DockerManager,
    workspace_path: Path,
    agent: str,
    image: str,
    agent_cfg: dict[str, Any],
    no_overlay: bool,
    rebuild_overlay: bool,
) -> str:
    """Swap in the project overlay image unless disabled by flag or config."""
    if no_overlay or _overlay_setting(workspace_path, agent, agent_cfg) is False:
        return image
    try:
        return overlay.apply_overlay(manager, workspace_path, agent, image, rebuild=rebuild_overlay)
    except DockerClientError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc


def agent_init_commands(agent: str, agent_cfg: dict[str, Any]) -> list[str]:
    """Read and validate per-agent init commands from config."""
    raw_init = agent_cfg.get("init", [])
    if raw_init is None:
        return []

    if isinstance(raw_init, str):
        items = [raw_init]
    elif isinstance(raw_init, list):
        items = raw_init
    else:
        raise typer.BadParameter(
            f"Invalid agents.{agent}.init value, expected a string or list of strings."
        )

    commands: list[str] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, str):
            raise typer.BadParameter(
                f"Invalid agents.{agent}.init[{index}] value, expected a string."
            )
        command = item.strip()
        if not command:
            raise typer.BadParameter(
                f"Invalid agents.{agent}.init[{index}] value, cannot be empty."
            )
        commands.append(command)
    return commands


def init_entrypoint(init_commands: list[str]) -> list[str]:
    """Build a shell entrypoint that runs init commands before the agent command."""
    script = "\n".join(
        [
            "set -e",
            *init_commands,
            'exec "$@"',
        ]
    )
    return ["/bin/sh", "-lc", script, "--"]


def agent_extra_volumes(agent: str, config_dir: Path) -> list[tuple[str, str, str]]:
    """Return agent-specific bind mounts as (host_path, container_path, mode)."""
    if agent == "auggie":
        host = str(config_dir / ".augment")
        return [
            (host, "/root/.augment", "rw"),
            (host, "/home/node/.augment", "rw"),
        ]
    if agent == "copilot":
        host = str(config_dir / ".copilot")
        return [
            (host, "/root/.copilot", "rw"),
            (host, "/home/node/.copilot", "rw"),
            (host, "/home/coder/.copilot", "rw"),
        ]
    if agent == "opencode":
        xdg_config = config_dir / ".config" / "opencode"
        xdg_data = config_dir / ".local" / "share" / "opencode"
        return [
            (str(xdg_data), "/root/.local/share/opencode", "rw"),
            (str(xdg_config), "/root/.config/opencode", "rw"),
            (str(xdg_data), "/home/node/.local/share/opencode", "rw"),
            (str(xdg_config), "/home/node/.config/opencode", "rw"),
        ]
    return []


X11_CONTAINER_XAUTH_PATH = "/tmp/.vibepod-xauth"


def prepare_x11_auth(display: str, config_dir: Path) -> Path | None:
    """Write an Xauthority file whose cookies work from inside the container.

    Host cookies are keyed to the host's hostname (FamilyLocal); the container
    has a different hostname, so the X server rejects its connections with
    "Authorization required, but no authorization protocol specified".
    Rewriting the address family to FamilyWild (0xffff) makes the cookie match
    regardless of hostname.
    """
    xauth = shutil.which("xauth")
    if xauth is None:
        return None
    try:
        nlist = subprocess.run(
            [xauth, "nlist", display],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    entries = [line for line in nlist.stdout.splitlines() if line.strip()]
    if nlist.returncode != 0 or not entries:
        return None

    wild = "".join(f"ffff{line[4:]}\n" for line in entries)
    auth_file = config_dir / "Xauthority"
    try:
        fd = os.open(auth_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
        os.chmod(auth_file, 0o600)
        merge = subprocess.run(
            [xauth, "-f", str(auth_file), "nmerge", "-"],
            input=wild,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if merge.returncode != 0:
        return None
    return auth_file


def x11_volumes_and_env(
    display: str, xauth_file: Path | None = None
) -> tuple[list[tuple[str, str, str]], dict[str, str]]:
    """Return X11 socket volumes and DISPLAY env for paste-image support."""
    volumes: list[tuple[str, str, str]] = [("/tmp/.X11-unix", "/tmp/.X11-unix", "rw")]
    env: dict[str, str] = {"DISPLAY": display}
    if xauth_file is not None:
        volumes.append((str(xauth_file), X11_CONTAINER_XAUTH_PATH, "ro"))
        env["XAUTHORITY"] = X11_CONTAINER_XAUTH_PATH
    return volumes, env


def host_user() -> str | None:
    """Return current user id in uid:gid format when available."""
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if not callable(getuid) or not callable(getgid):
        return None
    return f"{getuid()}:{getgid()}"


def host_identity_env() -> dict[str, str]:
    """Return host uid/gid env vars when the platform exposes POSIX user ids."""
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if not callable(getuid) or not callable(getgid):
        return {}
    return {
        "USER_UID": str(getuid()),
        "USER_GID": str(getgid()),
    }


def terminal_env_defaults() -> dict[str, str]:
    """Return host terminal-related env vars for interactive container apps."""
    keys = ("TERM", "COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "LANG")
    values = {key: value for key in keys if (value := os.environ.get(key))}
    values.setdefault("TERM", "xterm-256color")
    return values


def get_container_ip(container: Any, network: str) -> str | None:
    """Extract the container's IP address on the given Docker network."""
    try:
        network_settings = container.attrs.get("NetworkSettings")
        if not isinstance(network_settings, dict):
            return None
        networks = network_settings.get("Networks")
        if not isinstance(networks, dict):
            return None
        network_data = networks.get(network)
        if not isinstance(network_data, dict):
            return None
        ip = network_data.get("IPAddress")
        return ip if isinstance(ip, str) and ip else None
    except AttributeError:
        return None


def update_container_mapping(
    mapping_path: Path,
    ip: str,
    container_id: str,
    container_name: str,
    agent: str,
) -> bool:
    """Merge a new IP→container entry into containers.json atomically."""
    mapping: dict[str, dict[str, str]] = {}
    try:
        if mapping_path.exists():
            try:
                mapping = json.loads(mapping_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        mapping[ip] = {
            "container_id": container_id,
            "container_name": container_name,
            "agent": agent,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }

        tmp_path = mapping_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(mapping, indent=2))
        os.replace(tmp_path, mapping_path)
    except OSError:
        return False
    return True


_SAFE_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _is_safe_skill_id(skill_id: str) -> bool:
    """Return True for skill IDs safe to use as one container path segment."""
    return bool(_SAFE_SKILL_ID_RE.fullmatch(skill_id))


def _agent_skill_paths(agent: str) -> list[str]:
    """Container paths where each agent auto-discovers SKILL.md folders.

    All paths assume the in-container HOME or CONFIG_DIR conventions wired by
    vibepod-agents entrypoints. The SKILL.md format (Anthropic spec — frontmatter
    `name` + `description` + markdown body) is shared verbatim across claude,
    codex, pi, opencode, and auggie. They differ only in which directory they scan.

      - claude   reads $CLAUDE_CONFIG_DIR/skills/   → /claude/skills/
      - codex    reads ~/.agents/skills/            → /config/.agents/skills/
      - pi       reads ~/.pi/agent/skills/          → /config/.pi/agent/skills/
      - opencode reads ~/.agents/skills/ (also ~/.claude/skills/, ~/.config/opencode/skills/)
      - auggie   reads ~/.agents/skills/ (also ~/.augment/skills/, ~/.claude/skills/)
      - tau      reads ~/.agents/skills/ (also ~/.tau/skills/)
      - jcode    reads ~/.agents/skills/ (also ~/.jcode/skills/)

    Gemini wraps skills inside an extension manifest and would need a generated
    gemini-extension.json — handled separately when we add that support.
    Copilot CLI and Devstral Vibe have no documented SKILL.md auto-discovery.
    """
    if agent == "claude":
        return ["/claude/skills"]
    if agent == "pi":
        return ["/config/.pi/agent/skills"]
    if agent in ("codex", "opencode", "auggie", "tau", "jcode"):
        return ["/config/.agents/skills"]
    return []


def _resolved_skill_paths(workspace: Path) -> dict[str, Path]:
    """Merge installed skills from local + user scope (local wins).

    Returns id → absolute host path to the skill folder. Reads the lockfiles
    directly so this stays cheap during `vp run` (no engine container call).
    """
    from vibepod.core.skills_engine import local_skills_dir, user_skills_dir

    def _string_keyed_dict(value: object) -> dict[str, object] | None:
        if not isinstance(value, dict):
            return None
        result: dict[str, object] = {}
        for key, item in value.items():
            if isinstance(key, str):
                result[key] = item
        return result

    def _read_lock(path: Path) -> dict[str, object]:
        try:
            raw: object = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"skills": {}}
        return _string_keyed_dict(raw) or {"skills": {}}

    def _safe_skill_path(scope_root: Path, skill_id: str, path_value: object) -> Path | None:
        rel = path_value if isinstance(path_value, str) and path_value else f"installed/{skill_id}"
        rel_path = Path(rel)
        if rel_path.is_absolute() or ".." in rel_path.parts:
            rel_path = Path("installed") / skill_id
        abs_path = (scope_root / rel_path).resolve(strict=False)
        if not abs_path.is_relative_to(scope_root) or not abs_path.is_dir():
            return None
        return abs_path

    local_root = local_skills_dir(workspace).resolve()
    user_root = user_skills_dir().resolve()

    merged: dict[str, Path] = {}
    for scope_root in (user_root, local_root):  # local processed second → wins
        lock = _read_lock(scope_root / "skills-lock.json")
        skills = _string_keyed_dict(lock.get("skills"))
        if skills is None:
            continue
        for sid, raw_entry in skills.items():
            if not _is_safe_skill_id(sid):
                continue
            entry = _string_keyed_dict(raw_entry)
            if entry is None:
                continue
            abs_path = _safe_skill_path(scope_root, sid, entry.get("path"))
            if abs_path is not None:
                merged[sid] = abs_path
    return merged


def skills_mounts_for_agent(agent: str, workspace: Path) -> list[tuple[str, str, str]]:
    """One bind-mount per resolved skill into the agent's discovery path(s)."""
    targets = _agent_skill_paths(agent)
    if not targets:
        return []
    mounts: list[tuple[str, str, str]] = []
    for skill_id, host_path in _resolved_skill_paths(workspace).items():
        for base in targets:
            mounts.append((str(host_path), f"{base}/{skill_id}", "ro"))
    return mounts
