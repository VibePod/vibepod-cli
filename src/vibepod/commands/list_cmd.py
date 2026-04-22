"""List command implementation."""

from __future__ import annotations

from typing import Annotated, Any

import typer
from rich.table import Table

from vibepod.constants import DEFAULT_IMAGES, EXIT_DOCKER_NOT_RUNNING, SUPPORTED_AGENTS
from vibepod.core.agents import get_agent_shortcut
from vibepod.core.docker import DockerClientError, DockerManager
from vibepod.utils.console import console, error


def _configured_agent_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for agent in SUPPORTED_AGENTS:
        rows.append(
            {
                "short": get_agent_shortcut(agent) or "-",
                "agent": agent,
                "image": DEFAULT_IMAGES[agent],
            }
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
                "task_id": labels.get("vibepod.session_id", "-"),
            }
        )
    return sorted(rows, key=lambda row: (row["agent"], row["container"]))


def list_agents(
    running: Annotated[
        bool, typer.Option("-r", "--running", help="Show only running agents")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Output JSON")] = False,
) -> None:
    """List available agents and running containers."""
    try:
        manager = DockerManager()
        containers = manager.list_managed(all_containers=True)
    except DockerClientError as exc:
        if running:
            error(str(exc))
            raise typer.Exit(EXIT_DOCKER_NOT_RUNNING) from exc
        containers = []

    running_rows = _running_rows(containers)
    configured_rows = _configured_agent_rows()

    if as_json:
        import json

        payload: dict[str, Any] = {"running": running_rows}
        if not running:
            payload["agents"] = configured_rows
        print(json.dumps(payload, indent=2))
        return

    running_table = Table(title="Running Agents", title_justify="left")
    running_table.add_column("AGENT", style="cyan")
    running_table.add_column("CONTAINER", style="magenta")
    running_table.add_column("TASK ID")
    running_table.add_column("CONTEXT")

    if running_rows:
        for row in running_rows:
            running_table.add_row(row["agent"], row["container"], row["task_id"], row["context"])
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
