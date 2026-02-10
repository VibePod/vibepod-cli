"""List command implementation."""

from __future__ import annotations

from typing import Annotated, Any

import typer
from rich.table import Table

from vibepod.constants import DEFAULT_IMAGES, EXIT_DOCKER_NOT_RUNNING, SUPPORTED_AGENTS
from vibepod.core.docker import DockerClientError, DockerManager
from vibepod.utils.console import console, error


def _running_map(containers: list[Any]) -> dict[str, Any]:
    by_agent: dict[str, Any] = {}
    for container in containers:
        agent = container.labels.get("vibepod.agent")
        if agent and agent not in by_agent:
            by_agent[agent] = container
    return by_agent


def list_agents(
    running: Annotated[bool, typer.Option("-r", "--running", help="Show only running agents")] = False,
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

    mapped = _running_map(containers)
    rows: list[dict[str, str]] = []
    for agent in SUPPORTED_AGENTS:
        container = mapped.get(agent)
        rows.append(
            {
                "agent": agent,
                "image": DEFAULT_IMAGES[agent],
                "status": container.status if container else "stopped",
                "workspace": container.labels.get("vibepod.workspace", "-") if container else "-",
            }
        )

    if running:
        rows = [r for r in rows if r["status"] == "running"]

    if as_json:
        import json

        print(json.dumps(rows, indent=2))
        return

    table = Table(title="VibePod Agents")
    table.add_column("AGENT", style="cyan")
    table.add_column("IMAGE", style="magenta")
    table.add_column("STATUS")
    table.add_column("WORKSPACE")
    for row in rows:
        table.add_row(row["agent"], row["image"], row["status"], row["workspace"])
    console.print(table)
