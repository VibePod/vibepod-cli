"""Proxy subcommands."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from vibepod.constants import EXIT_DOCKER_NOT_RUNNING
from vibepod.core.config import get_config
from vibepod.core.docker import DockerClientError, DockerManager
from vibepod.utils.console import error, info, success, warning

app = typer.Typer(help="Manage the HTTP(S) proxy")


@app.command("start")
def proxy_start() -> None:
    """Start the proxy container."""
    config = get_config()
    proxy_cfg = config.get("proxy", {})

    proxy_image = str(proxy_cfg.get("image", "vibepod/proxy:latest"))
    db_path = (
        Path(str(proxy_cfg.get("db_path", "~/.config/vibepod/proxy/proxy.db")))
        .expanduser()
        .resolve()
    )
    ca_dir = (
        Path(str(proxy_cfg.get("ca_dir", "~/.config/vibepod/proxy/mitmproxy")))
        .expanduser()
        .resolve()
    )
    network_name = str(config.get("network", "vibepod-network"))

    try:
        manager = DockerManager()
    except DockerClientError as exc:
        error(str(exc))
        raise typer.Exit(EXIT_DOCKER_NOT_RUNNING) from exc

    manager.ensure_network(network_name)

    info("Starting proxy")
    manager.ensure_proxy(
        image=proxy_image,
        db_path=db_path,
        ca_dir=ca_dir,
        network=network_name,
    )
    success("Proxy is running")


@app.command("stop")
def proxy_stop(
    force: Annotated[bool, typer.Option("-f", "--force", help="Force stop")] = False,
) -> None:
    """Stop the proxy container."""
    try:
        manager = DockerManager()
    except DockerClientError as exc:
        error(str(exc))
        raise typer.Exit(EXIT_DOCKER_NOT_RUNNING) from exc

    existing = manager.find_proxy()
    if not existing:
        warning("Proxy is not running")
        return

    existing.stop(timeout=0 if force else 10)
    success("Proxy stopped")


@app.command("status")
def proxy_status() -> None:
    """Show proxy container status."""
    try:
        manager = DockerManager()
    except DockerClientError as exc:
        error(str(exc))
        raise typer.Exit(EXIT_DOCKER_NOT_RUNNING) from exc

    existing = manager.find_proxy()
    if not existing:
        info("Proxy is not running")
        return

    existing.reload()
    info(f"Proxy container: {existing.name} ({existing.status})")
