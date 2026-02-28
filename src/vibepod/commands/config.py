"""Config subcommands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
import yaml

from vibepod.core.config import get_config, get_global_config_path, get_project_config_path
from vibepod.utils.console import console, error, success

app = typer.Typer(help="Manage configuration")

PROJECT_CONFIG_MINIMAL = "version: 1\n"


@app.command("init")
def init(
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite existing project config if present")
    ] = False,
) -> None:
    """Create a minimal project config in the current directory."""
    project_path = get_project_config_path()
    if project_path.exists() and not force:
        error(f"Project config already exists: {project_path}")
        error("Use --force to overwrite.")
        raise typer.Exit(1)

    project_path.parent.mkdir(parents=True, exist_ok=True)
    project_path.write_text(PROJECT_CONFIG_MINIMAL, encoding="utf-8")
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
