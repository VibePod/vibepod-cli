"""Logs subcommands."""

from __future__ import annotations

import webbrowser
from pathlib import Path
from typing import Annotated

import typer

from vibepod.constants import EXIT_DOCKER_NOT_RUNNING
from vibepod.core.config import get_config
from vibepod.core.docker import DockerClientError, DockerManager
from vibepod.utils.console import error, info, success

app = typer.Typer(help="View logs and logging UI")


@app.command("ui")
def logs_ui(
    port: Annotated[int | None, typer.Option("--port", help="Datasette host port")] = None,
    no_open: Annotated[bool, typer.Option("--no-open", help="Do not open browser")] = False,
) -> None:
    """Start or reuse Datasette for API logs."""
    config = get_config()
    log_cfg = config.get("logging", {})

    datasette_image = str(log_cfg.get("image", "vibepod/datasette:latest"))
    datasette_port = port if port is not None else int(log_cfg.get("ui_port", 8001))
    db_path = Path(str(log_cfg.get("db_path", "~/.config/vibepod/logs.db"))).expanduser().resolve()

    try:
        manager = DockerManager()
    except DockerClientError as exc:
        error(str(exc))
        raise typer.Exit(EXIT_DOCKER_NOT_RUNNING) from exc

    info(f"Starting Datasette on http://localhost:{datasette_port}")
    manager.ensure_datasette(image=datasette_image, db_path=db_path, port=datasette_port)
    success("Datasette is ready")

    if not no_open:
        webbrowser.open(f"http://localhost:{datasette_port}")
