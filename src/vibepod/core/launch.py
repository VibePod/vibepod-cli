"""Helpers shared by `vp run` and `vp task run` for launching agent containers."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

from vibepod.utils.console import warning

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
        # fchmod overrides umask; os.open mode alone is umask-filtered
        os.fchmod(fd, 0o600)
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
    return []


def x11_volumes_and_env(display: str) -> tuple[list[tuple[str, str, str]], dict[str, str]]:
    """Return X11 socket volumes and DISPLAY env for paste-image support."""
    volumes: list[tuple[str, str, str]] = [("/tmp/.X11-unix", "/tmp/.X11-unix", "rw")]
    env: dict[str, str] = {"DISPLAY": display}
    return volumes, env


def host_user() -> str | None:
    """Return current user id in uid:gid format when available."""
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if not callable(getuid) or not callable(getgid):
        return None
    return f"{getuid()}:{getgid()}"


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
