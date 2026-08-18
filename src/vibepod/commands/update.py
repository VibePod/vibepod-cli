"""Update/version related commands."""

from __future__ import annotations

import platform
from typing import Annotated, Any

import typer

from vibepod import __version__
from vibepod.core.docker import (
    DockerClientError,
    DockerException,
    DockerManager,
    _version_is_podman,
)


def _runtime_info() -> tuple[str | None, str]:
    """Return (engine name, engine version) of the connected container runtime."""
    try:
        manager = DockerManager()
        version_info: dict[str, Any] = manager.client.version()
    except (DockerClientError, DockerException):
        # DockerException covers the engine disconnecting between ping() and version().
        return None, "unavailable"
    name = "Podman" if _version_is_podman(version_info) else "Docker"
    return name, str(version_info.get("Version", "unknown"))


def version(
    as_json: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Show version and runtime information."""
    runtime_name, runtime_version = _runtime_info()
    info = {
        "vibepod": __version__,
        "python": platform.python_version(),
        "runtime": runtime_name,
        # Kept for backward compatibility: the engine version regardless of runtime.
        "docker": runtime_version,
    }

    if as_json:
        import json

        print(json.dumps(info, indent=2))
        return

    runtime_display = f"{runtime_name} {runtime_version}" if runtime_name else runtime_version
    print(f"VibePod CLI: {info['vibepod']}")
    print(f"Python:      {info['python']}")
    print(f"Runtime:     {runtime_display}")
