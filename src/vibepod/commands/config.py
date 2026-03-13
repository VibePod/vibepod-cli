"""Config subcommands."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

from vibepod.constants import RUNTIME_AUTO, SUPPORTED_AGENTS, SUPPORTED_RUNTIMES
from vibepod.core.allowed_dirs import (
    add_allowed_dir,
    is_protected_dir,
    load_allowed_dirs,
    remove_allowed_dir,
)
from vibepod.core.config import get_config, get_global_config_path, get_project_config_path
from vibepod.core.runtime import get_saved_runtime_preference, save_runtime_preference
from vibepod.utils.console import console, error, success

app = typer.Typer(help="Manage configuration")

PROJECT_CONFIG_MINIMAL = "version: 1\n"


@app.command("init")
def init(
    agent: Annotated[
        str | None, typer.Argument(help="Optional agent config to copy into project")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite existing project config if present")
    ] = False,
) -> None:
    """Create a minimal project config or add a specific agent config."""
    project_path = get_project_config_path()
    if agent is not None:
        if agent not in SUPPORTED_AGENTS:
            error(f"Unknown agent '{agent}'. Supported: {', '.join(SUPPORTED_AGENTS)}")
            raise typer.Exit(1)

        try:
            project_path.parent.mkdir(parents=True, exist_ok=True)

            project_config: dict[str, Any] = {}
            if project_path.exists():
                loaded = yaml.safe_load(project_path.read_text(encoding="utf-8"))
                if loaded is None:
                    loaded = {}
                if not isinstance(loaded, dict):
                    error(f"Project config must contain a YAML mapping: {project_path}")
                    raise typer.Exit(1)
                project_config = loaded

            project_config.setdefault("version", 1)
            agents_config = project_config.setdefault("agents", {})
            if not isinstance(agents_config, dict):
                error(f"Project config key 'agents' must be a YAML mapping: {project_path}")
                raise typer.Exit(1)

            if agent in agents_config:
                error(f"Project config already contains agent '{agent}': {project_path}")
                raise typer.Exit(1)

            effective_agents = get_config().get("agents", {})
            if not isinstance(effective_agents, dict):
                error(f"Could not resolve config for agent '{agent}'.")
                raise typer.Exit(1)

            effective_agent = effective_agents.get(agent)
            if not isinstance(effective_agent, dict):
                error(f"Could not resolve config for agent '{agent}'.")
                raise typer.Exit(1)

            agents_config[agent] = copy.deepcopy(effective_agent)
            project_path.write_text(
                yaml.safe_dump(project_config, sort_keys=False),
                encoding="utf-8",
            )
        except OSError as exc:
            error(f"Failed to update project config at {project_path}: {exc}")
            raise typer.Exit(1) from exc
        except yaml.YAMLError as exc:
            error(f"Failed to parse project config at {project_path}: {exc}")
            raise typer.Exit(1) from exc

        success(f"Added agent config '{agent}' to project config: {project_path}")
        return

    if project_path.exists() and not force:
        error(f"Project config already exists: {project_path}")
        error("Use --force to overwrite.")
        raise typer.Exit(1)

    try:
        project_path.parent.mkdir(parents=True, exist_ok=True)
        project_path.write_text(PROJECT_CONFIG_MINIMAL, encoding="utf-8")
    except OSError as exc:
        error(f"Failed to create project config at {project_path}: {exc}")
        raise typer.Exit(1) from exc
    success(f"Created project config: {project_path}")


@app.command("show")
def show(
    as_json: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Show effective merged config."""
    cfg = get_config()
    if as_json:
        print(json.dumps(cfg, indent=2))
        return
    dumped = yaml.safe_dump(cfg, sort_keys=False)
    console.print(dumped)


@app.command("runtime")
def runtime(
    value: Annotated[
        str | None,
        typer.Argument(help="Default global runtime: auto, docker, or podman"),
    ] = None,
) -> None:
    """Show or set the saved default container runtime."""
    if value is None:
        try:
            print(get_saved_runtime_preference())
        except (OSError, ValueError) as exc:
            error(str(exc))
            raise typer.Exit(1) from exc
        except yaml.YAMLError as exc:
            error(f"Failed to parse global config at {get_global_config_path()}: {exc}")
            raise typer.Exit(1) from exc
        return

    allowed = (RUNTIME_AUTO, *SUPPORTED_RUNTIMES)
    normalized = value.strip().lower()
    if normalized not in allowed:
        error(f"Unknown runtime '{value}'. Supported: {', '.join(allowed)}")
        raise typer.Exit(1)

    try:
        save_runtime_preference(normalized)
    except OSError as exc:
        error(f"Failed to update global config at {get_global_config_path()}: {exc}")
        raise typer.Exit(1) from exc
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc
    except yaml.YAMLError as exc:
        error(f"Failed to parse global config at {get_global_config_path()}: {exc}")
        raise typer.Exit(1) from exc

    success(
        f"Set default container runtime to '{normalized}' in {get_global_config_path()}"
    )


@app.command("path")
def path(
    global_only: Annotated[
        bool, typer.Option("--global", help="Show global config path only")
    ] = False,
    project_only: Annotated[
        bool, typer.Option("--project", help="Show project config path only")
    ] = False,
) -> None:
    """Show config and logs paths."""
    global_path = get_global_config_path()
    project_path = get_project_config_path()

    if global_only and project_only:
        raise typer.BadParameter("Use only one of --global or --project")

    if global_only:
        print(global_path)
        return

    if project_only:
        print(project_path)
        return

    logs_path = Path(
        str(get_config().get("logging", {}).get("db_path", "~/.config/vibepod/logs.db"))
    )
    logs_path = logs_path.expanduser().resolve()

    print(f"Global:  {global_path}")
    print(f"Project: {project_path}")
    print(f"Logs:    {logs_path}")


@app.command("allow-dir")
def allow_dir(
    directory: Annotated[
        Path | None,
        typer.Argument(help="Directory to allow (defaults to current directory)"),
    ] = None,
) -> None:
    """Add a directory to the vp run allow list."""
    try:
        target = (directory or Path.cwd()).expanduser().resolve()
    except (OSError, ValueError) as exc:
        error(f"Could not resolve directory path: {exc}")
        raise typer.Exit(1) from exc
    if not target.exists() or not target.is_dir():
        error(f"Not a valid directory: {target}")
        raise typer.Exit(1)
    if is_protected_dir(target):
        error(
            f"'{target}' is a protected directory (home or root) and cannot be added "
            "to the allow list."
        )
        raise typer.Exit(1)
    try:
        add_allowed_dir(target)
    except OSError as exc:
        error(f"Could not update allow list: {exc}")
        raise typer.Exit(1) from exc
    success(f"Allowed: {target}")


@app.command("remove-dir")
def remove_dir(
    directory: Annotated[
        Path | None,
        typer.Argument(help="Directory to remove (defaults to current directory)"),
    ] = None,
) -> None:
    """Remove a directory from the vp run allow list."""
    try:
        target = (directory or Path.cwd()).expanduser().resolve()
    except (OSError, ValueError) as exc:
        error(f"Could not resolve directory path: {exc}")
        raise typer.Exit(1) from exc
    try:
        removed = remove_allowed_dir(target)
    except OSError as exc:
        error(f"Could not update allow list: {exc}")
        raise typer.Exit(1) from exc
    if removed:
        success(f"Removed: {target}")
    else:
        error(f"Directory not in allow list: {target}")
        raise typer.Exit(1)


@app.command("list-allowed-dirs")
def list_allowed_dirs() -> None:
    """List all directories in the vp run allow list."""
    dirs = load_allowed_dirs()
    if not dirs:
        console.print("No directories in the allow list.")
        return
    for d in dirs:
        console.print(d)
