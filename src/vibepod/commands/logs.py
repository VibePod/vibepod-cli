"""Logs subcommands."""

from __future__ import annotations

import webbrowser
from pathlib import Path
from typing import Annotated

import typer

from vibepod.constants import EXIT_DOCKER_NOT_RUNNING
from vibepod.core.config import get_config
from vibepod.core.docker import DockerClientError, DockerManager
from vibepod.utils.console import error, info, success, warning

app = typer.Typer(help="View logs and traffic UI")


@app.command("start")
def logs_start(
    port: Annotated[int | None, typer.Option("--port", help="Datasette host port")] = None,
    no_open: Annotated[bool, typer.Option("--no-open", help="Do not open browser")] = False,
) -> None:
    """Start or reuse Datasette for session and proxy logs."""
    config = get_config()
    log_cfg = config.get("logging", {})
    proxy_cfg = config.get("proxy", {})

    datasette_image = str(log_cfg.get("image", "vibepod/datasette:latest"))
    datasette_port = port if port is not None else int(log_cfg.get("ui_port", 8001))
    logs_db_path = Path(str(log_cfg.get("db_path", "~/.config/vibepod/logs.db"))).expanduser()
    proxy_db_path = Path(
        str(proxy_cfg.get("db_path", "~/.config/vibepod/proxy/proxy.db"))
    ).expanduser()

    try:
        manager = DockerManager()
    except DockerClientError as exc:
        error(str(exc))
        raise typer.Exit(EXIT_DOCKER_NOT_RUNNING) from exc

    info(f"Starting Datasette on http://localhost:{datasette_port}")
    manager.ensure_datasette(
        image=datasette_image,
        logs_db_path=logs_db_path,
        proxy_db_path=proxy_db_path,
        port=datasette_port,
    )
    success("Datasette is ready")

    if not no_open:
        webbrowser.open(f"http://localhost:{datasette_port}")


@app.command("stop")
def logs_stop(
    force: Annotated[bool, typer.Option("-f", "--force", help="Force stop")] = False,
) -> None:
    """Stop the Datasette container."""
    try:
        manager = DockerManager()
    except DockerClientError as exc:
        error(str(exc))
        raise typer.Exit(EXIT_DOCKER_NOT_RUNNING) from exc

    existing = manager.find_datasette()
    if not existing:
        warning("Datasette is not running")
        return

    existing.stop(timeout=0 if force else 10)
    success("Datasette stopped")


@app.command("status")
def logs_status() -> None:
    """Show Datasette container status."""
    try:
        manager = DockerManager()
    except DockerClientError as exc:
        error(str(exc))
        raise typer.Exit(EXIT_DOCKER_NOT_RUNNING) from exc

    existing = manager.find_datasette()
    if not existing:
        info("Datasette is not running")
        return

    existing.reload()
    info(f"Datasette container: {existing.name} ({existing.status})")


@app.command("ui", hidden=True)
def logs_ui(
    port: Annotated[int | None, typer.Option("--port", help="Datasette host port")] = None,
    no_open: Annotated[bool, typer.Option("--no-open", help="Do not open browser")] = False,
) -> None:
    """Alias for `vp logs start`."""
    logs_start(port=port, no_open=no_open)
