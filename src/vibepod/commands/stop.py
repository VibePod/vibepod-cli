"""Stop command implementation."""

from __future__ import annotations

from typing import Annotated

import typer

from vibepod.constants import EXIT_DOCKER_NOT_RUNNING
from vibepod.core.agents import resolve_agent_name
from vibepod.core.docker import DockerClientError, DockerManager
from vibepod.utils.console import error, success


def stop(
    target: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Agent name/shortcut (stops all its containers) or a container "
                "name or ID from `vp list` (stops just that container)."
            ),
        ),
    ] = None,
    all_containers: Annotated[
        bool,
        typer.Option("-a", "--all", help="Stop all VibePod managed containers"),
    ] = False,
    force: Annotated[bool, typer.Option("-f", "--force", help="Force stop")] = False,
) -> None:
    """Stop an agent's containers, a specific container, or all managed containers."""
    if not all_containers and target is None:
        raise typer.BadParameter("Provide an AGENT or CONTAINER, or use --all")

    try:
        manager = DockerManager()
    except DockerClientError as exc:
        error(str(exc))
        raise typer.Exit(EXIT_DOCKER_NOT_RUNNING) from exc

    if all_containers:
        try:
            stopped = manager.stop_all(force=force)
        except DockerClientError as exc:
            error(str(exc))
            raise typer.Exit(1) from exc
        success(f"Stopped {stopped} container(s)")
        return

    assert target is not None
    resolved_agent = resolve_agent_name(target)
    if resolved_agent is not None:
        try:
            stopped = manager.stop_agent(agent=resolved_agent, force=force)
        except DockerClientError as exc:
            error(str(exc))
            raise typer.Exit(1) from exc
        success(f"Stopped {stopped} container(s) for {resolved_agent}")
        return

    try:
        container = manager.stop_container(target, force=force)
    except DockerClientError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc
    success(f"Stopped {container.name}")
