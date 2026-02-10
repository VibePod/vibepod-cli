"""Stop command implementation."""

from __future__ import annotations

from typing import Annotated

import typer

from vibepod.constants import EXIT_DOCKER_NOT_RUNNING
from vibepod.core.docker import DockerClientError, DockerManager
from vibepod.utils.console import error, success


def stop(
    agent: Annotated[str | None, typer.Argument(help="Agent to stop")] = None,
    all_containers: Annotated[
        bool,
        typer.Option("-a", "--all", help="Stop all VibePod managed containers"),
    ] = False,
    force: Annotated[bool, typer.Option("-f", "--force", help="Force stop")] = False,
) -> None:
    """Stop one agent container, or all managed containers."""
    if not all_containers and agent is None:
        raise typer.BadParameter("Provide an AGENT or use --all")

    try:
        manager = DockerManager()
    except DockerClientError as exc:
        error(str(exc))
        raise typer.Exit(EXIT_DOCKER_NOT_RUNNING) from exc

    if all_containers:
        stopped = manager.stop_all(force=force)
        success(f"Stopped {stopped} container(s)")
        return

    assert agent is not None
    stopped = manager.stop_agent(agent=agent, force=force)
    success(f"Stopped {stopped} container(s) for {agent}")
