"""Update/version related commands."""

from __future__ import annotations

import platform
from typing import Annotated, Any

import typer

from vibepod import __version__
from vibepod.core.config import get_config
from vibepod.core.docker import DockerClientError, get_manager


def _runtime_version(runtime_override: str | None = None) -> tuple[str, str]:
    """Return (runtime_name, version_string)."""
    try:
        manager = get_manager(runtime_override=runtime_override, config=get_config())
        version_info: dict[str, Any] = manager.client.version()
        return manager.runtime, str(version_info.get("Version", "unknown"))
    except DockerClientError:
        return "unknown", "unavailable"


def version(
    as_json: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    runtime: Annotated[
        str | None,
        typer.Option("--runtime", help="Container runtime to use (docker or podman)"),
    ] = None,
) -> None:
    """Show version and runtime information."""
    rt_name, rt_version = _runtime_version(runtime_override=runtime)
    info = {
        "vibepod": __version__,
        "python": platform.python_version(),
        "runtime": rt_name,
        "runtime_version": rt_version,
    }

    if as_json:
        import json

        print(json.dumps(info, indent=2))
        return

    print(f"VibePod CLI: {info['vibepod']}")
    print(f"Python:      {info['python']}")
    print(f"Runtime:     {info['runtime']} {info['runtime_version']}")
