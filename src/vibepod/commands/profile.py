"""Profile subcommands: manage named agent credential environments."""

from __future__ import annotations

import os
from typing import Annotated, Any

import typer

from vibepod.constants import SUPPORTED_AGENTS
from vibepod.core.config import get_config
from vibepod.core.docker import DockerClientError, DockerManager
from vibepod.core.launch import managed_proxy_policy_ids
from vibepod.core.profiles import (
    DEFAULT_PROFILE,
    create_profile,
    list_profiles,
    profile_agents_root,
    profile_exists,
    remove_profile,
    resolve_profile,
    validate_profile_name,
)
from vibepod.core.proxy_filter import cleanup_orphan_policies
from vibepod.utils.console import console, error, success, warning

app = typer.Typer(help="Manage credential profiles (separate agent logins per environment)")


def _agents_with_data(profile: str) -> list[str]:
    """Agents with any stored data (config, caches, credentials) in the profile."""
    root = profile_agents_root(profile)
    found: list[str] = []
    for agent in sorted(SUPPORTED_AGENTS):
        agent_dir = root / agent
        if agent_dir.is_dir() and any(agent_dir.iterdir()):
            found.append(agent)
    return found


def _active_profile() -> str | None:
    """Resolved active profile, or None when the current selection is broken."""
    try:
        return resolve_profile(None, get_config())
    except ValueError as exc:
        warning(str(exc))
        return None


def _cleanup_removed_profile_policy(config: dict[str, Any]) -> None:
    """Sweep only when the complete managed-container set is available."""
    try:
        manager = DockerManager()
    except DockerClientError:
        return
    referenced = managed_proxy_policy_ids(manager)
    if referenced is not None:
        cleanup_orphan_policies(config, referenced)


@app.command("list")
def list_() -> None:
    """List profiles, the active one, and which agents have stored data."""
    active = _active_profile()
    for name in list_profiles():
        marker = "*" if name == active else " "
        agents = ", ".join(_agents_with_data(name))
        suffix = f"  ({agents})" if agents else ""
        console.print(f"{marker} {name}{suffix}")


@app.command("create")
def create(
    name: Annotated[str, typer.Argument(help="Profile name (lowercase slug)")],
) -> None:
    """Create a new empty profile."""
    try:
        path = create_profile(name)
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(code=1) from exc
    except OSError as exc:
        error(f"Could not create profile '{name}': {exc}")
        raise typer.Exit(code=1) from exc
    success(f"Created profile '{name}' at {path.parent}")


@app.command("remove")
def remove(
    name: Annotated[str, typer.Argument(help="Profile to remove")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Remove a profile and its stored credentials."""
    if name == DEFAULT_PROFILE:
        error("Profile 'default' cannot be removed.")
        raise typer.Exit(code=1)
    try:
        validate_profile_name(name)
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(code=1) from exc
    if not profile_exists(name):
        error(f"Profile '{name}' does not exist.")
        raise typer.Exit(code=1)
    if not yes:
        typer.confirm(f"Remove profile '{name}' and all credentials stored in it?", abort=True)
    config = get_config()
    try:
        remove_profile(name)
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(code=1) from exc
    except OSError as exc:
        error(
            f"Could not remove profile '{name}': {exc}. The profile may be partially "
            "deleted. Files created by agent containers can be owned by another user; "
            "fix ownership (e.g. `sudo chown -R $USER ...`) and retry.",
        )
        raise typer.Exit(code=1) from exc
    if os.environ.get("VP_PROFILE") == name:
        console.print(f"Note: VP_PROFILE still points at removed profile '{name}'.")
    _cleanup_removed_profile_policy(config)
    success(f"Removed profile '{name}'")
