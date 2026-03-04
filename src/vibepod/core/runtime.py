"""Container runtime detection and selection (Docker / Podman)."""

from __future__ import annotations

import os
import sys
from typing import Any

import yaml

from vibepod.constants import (
    DOCKER_SOCKET,
    PODMAN_SOCKET_ROOTFUL,
    PODMAN_SOCKET_ROOTLESS,
    RUNTIME_AUTO,
    RUNTIME_DOCKER,
    RUNTIME_PODMAN,
    SUPPORTED_RUNTIMES,
)
from vibepod.core.config import get_config_root


def _socket_candidates() -> list[tuple[str, str]]:
    """Return (runtime_name, socket_url) pairs to probe.

    For Podman the rootless socket is preferred.  ``XDG_RUNTIME_DIR`` is
    checked first (works on all distros), then the conventional
    ``/run/user/{uid}`` path.  The rootful socket is only included when
    running as root, since it requires elevated privileges otherwise.
    """
    candidates: list[tuple[str, str]] = [(RUNTIME_DOCKER, DOCKER_SOCKET)]

    uid = os.getuid() if hasattr(os, "getuid") else 1000

    # Rootless Podman — prefer XDG_RUNTIME_DIR, fall back to /run/user/{uid}
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        candidates.append(
            (RUNTIME_PODMAN, f"unix://{xdg_runtime}/podman/podman.sock")
        )
    candidates.append(
        (RUNTIME_PODMAN, PODMAN_SOCKET_ROOTLESS.format(uid=uid))
    )

    # Rootful Podman — only useful when already running as root
    if uid == 0:
        candidates.append((RUNTIME_PODMAN, PODMAN_SOCKET_ROOTFUL))

    return candidates


def _probe_socket(base_url: str) -> bool:
    """Return True if a container engine responds on *base_url*."""
    try:
        import docker as _docker

        client = _docker.DockerClient(base_url=base_url, timeout=3)
        client.ping()
        client.close()
        return True
    except Exception:
        return False


def detect_available_runtimes() -> dict[str, str]:
    """Detect which container runtimes are reachable.

    Returns a dict mapping runtime name to the first working socket URL,
    e.g. ``{"docker": "unix:///var/run/docker.sock"}``.
    """
    found: dict[str, str] = {}
    for name, url in _socket_candidates():
        if name in found:
            continue
        if _probe_socket(url):
            found[name] = url
    return found


def save_runtime_preference(runtime: str) -> None:
    """Persist *runtime* as ``container_runtime`` in the global config file."""
    config_path = get_config_root() / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, Any] = {}
    if config_path.exists():
        content = config_path.read_text(encoding="utf-8")
        if content.strip():
            loaded = yaml.safe_load(content)
            if isinstance(loaded, dict):
                existing = loaded

    existing["container_runtime"] = runtime
    config_path.write_text(yaml.safe_dump(existing, sort_keys=False), encoding="utf-8")


def resolve_runtime(
    override: str | None = None,
    config: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Determine which container runtime and socket to use.

    Priority:
    1. *override* (``--runtime`` CLI flag)
    2. ``VP_CONTAINER_RUNTIME`` env var
    3. ``container_runtime`` config key (saved preference)
    4. Auto-detect: single runtime → use it; both → prompt (or Docker in non-TTY)

    Returns ``(runtime_name, socket_url)``.

    Raises ``RuntimeError`` when no runtime is available.
    """
    from rich.prompt import Prompt

    cfg = config or {}

    # --- explicit choice (flag → env → config) ---
    explicit: str | None = override
    if explicit is None:
        explicit = os.environ.get("VP_CONTAINER_RUNTIME")
    if explicit is None:
        saved = cfg.get("container_runtime", RUNTIME_AUTO)
        if saved != RUNTIME_AUTO:
            explicit = str(saved)

    available = detect_available_runtimes()

    if explicit is not None:
        explicit = explicit.lower()
        if explicit not in SUPPORTED_RUNTIMES:
            raise RuntimeError(
                f"Unknown container runtime '{explicit}'. "
                f"Supported: {', '.join(SUPPORTED_RUNTIMES)}"
            )
        if explicit not in available:
            raise RuntimeError(
                f"Container runtime '{explicit}' is not available. "
                "Is the daemon/service running?"
            )
        return explicit, available[explicit]

    # --- auto-detect ---
    if not available:
        raise RuntimeError(
            "No container runtime found. Install and start Docker or Podman."
        )

    if len(available) == 1:
        name = next(iter(available))
        return name, available[name]

    # Both available — prompt if interactive, else default to Docker
    if sys.stdin.isatty():
        choice = Prompt.ask(
            "Multiple container runtimes detected. Which one should VibePod use?",
            choices=list(SUPPORTED_RUNTIMES),
            default=RUNTIME_DOCKER,
        )
        save_runtime_preference(choice)
        return choice, available[choice]

    return RUNTIME_DOCKER, available[RUNTIME_DOCKER]

