"""Stop command implementation."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated, Any

import typer

from vibepod.constants import EXIT_DOCKER_NOT_RUNNING
from vibepod.core.agents import resolve_agent_name
from vibepod.core.config import get_config
from vibepod.core.dash import AGENT_ID_LABEL as DASH_ID_LABEL
from vibepod.core.dash import report as dash_report
from vibepod.core.dash import target_from_labels
from vibepod.core.docker import DockerClientError, DockerManager
from vibepod.core.herdr import PANE_LABEL, release_agent
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
        _release_agent_entries(_managed_containers(manager))
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
        _release_agent_entries(
            c
            for c in _managed_containers(manager)
            if (getattr(c, "labels", {}) or {}).get("vibepod.agent") == resolved_agent
        )
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
    _release_agent_entries([container])
    success(f"Stopped {container.name}")


def _managed_containers(manager: Any) -> list[Any]:
    lister = getattr(manager, "list_managed", None)
    if not callable(lister):
        return []
    try:
        return list(lister(all_containers=True))
    except DockerClientError:
        return []


def _release_agent_entries(containers: Iterable[Any]) -> None:
    """Tell herdr and the dashboard that these containers are finished."""
    containers = list(containers)
    _release_herdr_entries(containers)
    _report_dash_stopped(containers)


def _release_herdr_entries(containers: Iterable[Any]) -> None:
    """Clear herdr sidebar entries for containers started inside herdr panes."""
    for container in containers:
        labels = getattr(container, "labels", {}) or {}
        pane = labels.get(PANE_LABEL)
        agent = labels.get("vibepod.agent")
        if pane and agent:
            release_agent(agent, pane=pane)


def _report_dash_stopped(containers: Iterable[Any]) -> None:
    """Mark dashboard cards done for containers started with dash wiring."""
    config: dict[str, Any] | None = None
    for container in containers:
        labels = getattr(container, "labels", {}) or {}
        if not labels.get(DASH_ID_LABEL):
            continue
        if config is None:
            config = get_config()
        target = target_from_labels(labels, config)
        if target is not None:
            dash_report(target, "done", event="container.stop", message="stopped by vp stop")
