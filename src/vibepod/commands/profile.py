"""Profile subcommands: manage named agent credential environments."""

from __future__ import annotations

import os
from typing import Annotated

import typer

from vibepod.constants import SUPPORTED_AGENTS
from vibepod.core.config import get_config
from vibepod.core.profiles import (
    DEFAULT_PROFILE,
    create_profile,
    list_profiles,
    profile_agents_root,
    remove_profile,
    resolve_profile,
)
from vibepod.utils.console import console, error, success

app = typer.Typer(help="Manage credential profiles (separate agent logins per environment)")


def _agents_with_credentials(profile: str) -> list[str]:
    root = profile_agents_root(profile)
    found: list[str] = []
    for agent in sorted(SUPPORTED_AGENTS):
        agent_dir = root / agent
        if agent_dir.is_dir() and any(agent_dir.iterdir()):
            found.append(agent)
    return found


def _active_profile() -> str:
    try:
        return resolve_profile(None, get_config())
    except ValueError:
        return DEFAULT_PROFILE


@app.command("list")
def list_() -> None:
    """List profiles, the active one, and which agents have credentials."""
    active = _active_profile()
    for name in list_profiles():
        marker = "*" if name == active else " "
        agents = ", ".join(_agents_with_credentials(name))
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
    if not yes:
        typer.confirm(
            f"Remove profile '{name}' and all credentials stored in it?", abort=True
        )
    try:
        remove_profile(name)
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(code=1) from exc
    if os.environ.get("VP_PROFILE") == name:
        console.print(f"Note: VP_PROFILE still points at removed profile '{name}'.")
    success(f"Removed profile '{name}'")
