"""List command implementation."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer
from rich.table import Table

from vibepod.constants import DEFAULT_IMAGES, EXIT_DOCKER_NOT_RUNNING, SUPPORTED_AGENTS
from vibepod.core import overlay
from vibepod.core.agents import effective_agent_image, get_agent_shortcut
from vibepod.core.config import get_config
from vibepod.core.docker import DockerClientError, DockerManager
from vibepod.core.launch import overlay_enabled
from vibepod.utils.console import console, error


def _configured_agent_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for agent in SUPPORTED_AGENTS:
        rows.append(
            {
                "short": get_agent_shortcut(agent) or "-",
                "agent": agent,
                "image": DEFAULT_IMAGES[agent],
            },
        )
    return rows


def _running_rows(containers: list[Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for container in containers:
        labels = getattr(container, "labels", {}) or {}
        agent = labels.get("vibepod.agent")
        status = getattr(container, "status", "-")
        if not agent or status != "running":
            continue
        rows.append(
            {
                "agent": agent,
                "container": getattr(container, "name", "-"),
                "context": labels.get("vibepod.workspace", "-"),
            },
        )
    return sorted(rows, key=lambda row: (row["agent"], row["container"]))


def _overlay_rows(manager: DockerManager | None) -> list[dict[str, str]]:
    """Overlay images the current project resolves to, one row per agent.

    Reports what a launch from here would use — including whether the image
    is already built — so the opaque content hashes in ``docker images`` can
    be traced back to a project without guessing.

    Every agent with an overlay gets a row even when the answer is not a tag:
    omitting the undecidable ones would read as "this project has no overlay
    for that agent".
    """
    workspace = Path.cwd()
    config = get_config()
    rows: list[dict[str, str]] = []
    for agent in SUPPORTED_AGENTS:
        if overlay.find_overlay_dockerfile(workspace, agent) is None:
            continue
        agent_cfg = config.get("agents", {}).get(agent) or {}
        if not overlay_enabled(workspace, agent, agent_cfg):
            # Config alone decides this, so it is still reportable with no
            # working docker.
            rows.append({"agent": agent, "image": "-", "state": "disabled"})
            continue
        if manager is None:
            rows.append({"agent": agent, "image": "-", "state": "docker unavailable"})
            continue
        try:
            resolved = overlay.resolve_overlay_image(
                manager,
                workspace,
                agent,
                effective_agent_image(agent, config),
            )
        except (DockerClientError, OSError):
            # Listing is read-only: an unreadable overlay context or a
            # hiccupping daemon must not take the whole command down.
            continue
        if resolved is None:
            continue
        if not resolved.base_local:
            # The digest follows the base image id, which `vp run` pulls
            # before building. Naming a tag derived from the bare image
            # string would print one no launch ever produces.
            rows.append(
                {"agent": agent, "image": resolved.repository, "state": "base not pulled"},
            )
            continue
        rows.append(
            {
                "agent": agent,
                "image": resolved.tag,
                "state": "built" if resolved.built else "not built",
            },
        )
    return rows


def list_agents(
    running: Annotated[
        bool,
        typer.Option("-r", "--running", help="Show only running agents"),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Output JSON")] = False,
) -> None:
    """List available agents and running containers."""
    manager: DockerManager | None = None
    try:
        manager = DockerManager()
        containers = manager.list_managed(all_containers=True)
    except DockerClientError as exc:
        if running:
            error(str(exc))
            raise typer.Exit(EXIT_DOCKER_NOT_RUNNING) from exc
        manager = None
        containers = []

    running_rows = _running_rows(containers)
    configured_rows = _configured_agent_rows()
    overlay_rows = [] if running else _overlay_rows(manager)

    if as_json:
        import json

        payload: dict[str, Any] = {"running": running_rows}
        if not running:
            payload["agents"] = configured_rows
            payload["overlays"] = overlay_rows
        print(json.dumps(payload, indent=2))
        return

    running_table = Table(title="Running Agents", title_justify="left")
    running_table.add_column("AGENT", style="cyan")
    running_table.add_column("CONTAINER", style="magenta")
    running_table.add_column("CONTEXT")

    if running_rows:
        for row in running_rows:
            running_table.add_row(row["agent"], row["container"], row["context"])
        console.print(running_table)
    else:
        console.print("No running agents.")

    if running:
        return

    console.print()
    reference_table = Table(title="Configured Agents", title_justify="left")
    reference_table.add_column("SHORT", style="green")
    reference_table.add_column("AGENT", style="cyan")
    reference_table.add_column("BASE IMAGE", style="magenta")
    for row in configured_rows:
        reference_table.add_row(row["short"], row["agent"], row["image"])
    console.print(reference_table)

    if not overlay_rows:
        return

    console.print()
    overlay_table = Table(title="Project Overlays", title_justify="left")
    overlay_table.add_column("AGENT", style="cyan")
    # fold, not the default ellipsis: a truncated tag cannot be copied into a
    # docker command.
    overlay_table.add_column("OVERLAY IMAGE", style="magenta", overflow="fold")
    overlay_table.add_column("STATE")
    for row in overlay_rows:
        overlay_table.add_row(row["agent"], row["image"], row["state"])
    console.print(overlay_table)
