"""Update/version related commands."""

from __future__ import annotations

import platform
from typing import Annotated, Any

import typer

from vibepod import __version__
from vibepod.core.docker import DockerClientError, DockerManager


def _docker_version() -> str:
    try:
        manager = DockerManager()
        version_info: dict[str, Any] = manager.client.version()
        return str(version_info.get("Version", "unknown"))
    except DockerClientError:
        return "unavailable"


def version(
    as_json: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Show version and runtime information."""
    info = {
        "vibepod": __version__,
        "python": platform.python_version(),
        "docker": _docker_version(),
    }

    if as_json:
        import json

        print(json.dumps(info, indent=2))
        return

    print(f"VibePod CLI: {info['vibepod']}")
    print(f"Python:      {info['python']}")
    print(f"Docker:      {info['docker']}")
