"""Attach command implementation."""

from __future__ import annotations

from typing import Annotated

import typer

from vibepod.constants import CONTAINER_LABEL_MANAGED, EXIT_DOCKER_NOT_RUNNING
from vibepod.core.docker import DockerClientError, DockerManager
from vibepod.utils.console import error, info, warning


def attach(
    container: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Container name or ID to attach to (see `vp list`). "
                "Omit when exactly one managed container is running."
            ),
        ),
    ] = None,
) -> None:
    """Reattach your terminal to a running VibePod-managed container.

    Use this to rejoin an agent session after the terminal that started it
    was closed. Find candidate containers with `vp list`.
    """
    try:
        manager = DockerManager()
    except DockerClientError as exc:
        error(str(exc))
        raise typer.Exit(EXIT_DOCKER_NOT_RUNNING) from exc

    if container is None:
        try:
            managed = manager.list_managed()
        except DockerClientError as exc:
            error(str(exc))
            raise typer.Exit(1) from exc
        running = [
            c
            for c in managed
            if getattr(c, "status", "") == "running"
            and (getattr(c, "labels", {}) or {}).get("vibepod.agent")
        ]
        if not running:
            error(
                "No running VibePod agent containers to attach to. "
                "Start one with `vp run`, or check `vp list --running`."
            )
            raise typer.Exit(1)
        if len(running) > 1:
            names = ", ".join(sorted(c.name for c in running))
            error(
                f"Multiple running containers: {names}. "
                "Specify one explicitly: `vp attach <container>`."
            )
            raise typer.Exit(1)
        target = running[0]
    else:
        try:
            target = manager.get_container(container)
        except DockerClientError as exc:
            error(str(exc))
            raise typer.Exit(1) from exc

        labels = getattr(target, "labels", {}) or {}
        if labels.get(CONTAINER_LABEL_MANAGED) != "true":
            error(f"Container '{container}' is not managed by VibePod.")
            raise typer.Exit(1)
        if getattr(target, "status", "") != "running":
            error(
                f"Container '{container}' is not running "
                f"(status: {getattr(target, 'status', 'unknown')})."
            )
            raise typer.Exit(1)

    agent = (getattr(target, "labels", {}) or {}).get("vibepod.agent", "agent")
    info(f"Attaching to {target.name} ({agent})")
    warning(
        f"Close the terminal to leave it running, or stop it with `vp stop {target.name}`."
    )
    try:
        manager.attach_interactive(target)
    except DockerClientError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc
