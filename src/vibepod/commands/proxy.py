"""Proxy subcommands."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from vibepod.constants import EXIT_DOCKER_NOT_RUNNING
from vibepod.core.config import get_config
from vibepod.core.docker import DockerClientError, DockerManager, _is_latest_tag
from vibepod.core.proxy_filter import (
    VALID_MODES,
    get_filter_settings,
    normalize_pattern,
    raw_configured_mode,
    update_global_filter,
    write_filter_file,
)
from vibepod.utils.console import error, info, success, warning

app = typer.Typer(help="Manage the HTTP(S) proxy")

filter_app = typer.Typer(help="Manage proxy allow/deny filtering")
allow_app = typer.Typer(help="Manage the allow list")
deny_app = typer.Typer(help="Manage the deny list")
filter_app.add_typer(allow_app, name="allow")
filter_app.add_typer(deny_app, name="deny")
app.add_typer(filter_app, name="filter")


def _sync_filter_file() -> None:
    write_filter_file(get_config())


def _normalized(entry: object) -> str:
    return str(entry).strip().lower().rstrip(".")


_OVERRIDE_HINT = "overridden by project config (.vibepod/config.yaml) or VP_PROXY_FILTER_MODE"


def _warn_invalid_configured_mode(config: dict[str, Any]) -> None:
    raw = raw_configured_mode(config)
    if raw.strip().lower() not in VALID_MODES:
        warning(f"Invalid proxy.filter.mode '{raw}' in config; treating as 'open'")


@filter_app.command("status")
def filter_status() -> None:
    """Show filter mode, lists, and proxy state."""
    config = get_config()
    settings = get_filter_settings(config)
    _warn_invalid_configured_mode(config)
    info(f"Mode: {settings['mode']}")
    info(f"Allow list ({len(settings['allow'])}): {', '.join(settings['allow']) or '—'}")
    info(f"Deny list ({len(settings['deny'])}): {', '.join(settings['deny']) or '—'}")
    try:
        manager = DockerManager()
    except DockerClientError:
        info("Proxy: unknown (Docker is not running)")
        return
    existing = manager.find_proxy()
    if existing is None:
        info("Proxy: not running")
    else:
        existing.reload()
        info(f"Proxy: {existing.name} ({existing.status})")


@filter_app.command("mode")
def filter_mode(
    value: Annotated[str, typer.Argument(help="open, allow, or deny")],
) -> None:
    """Set the filter mode (open = no filtering)."""
    normalized = value.strip().lower()
    if normalized not in VALID_MODES:
        error(f"Unknown mode '{value}'. Valid modes: {', '.join(VALID_MODES)}")
        raise typer.Exit(1)
    update_global_filter(lambda f: f.update(mode=normalized))
    _sync_filter_file()
    success(f"Proxy filter mode set to '{normalized}'")
    effective = get_filter_settings(get_config())
    if effective["mode"] != normalized:
        warning(
            f"Effective mode stays '{effective['mode']}' — {_OVERRIDE_HINT}",
        )


def _list_add(list_name: str, host: str) -> None:
    try:
        pattern = normalize_pattern(host)
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    added = True

    def mutate(filter_cfg: dict[str, Any]) -> None:
        nonlocal added
        entries = filter_cfg.setdefault(list_name, [])
        if not isinstance(entries, list):
            entries = []
            filter_cfg[list_name] = entries
        # Hand-authored config entries may be unnormalized; compare normalized.
        if pattern in {_normalized(e) for e in entries}:
            added = False
            return
        entries.append(pattern)

    update_global_filter(mutate)
    if not added:
        warning(f"'{pattern}' is already in the {list_name} list")
        return
    _sync_filter_file()
    success(f"Added '{pattern}' to the {list_name} list")
    if pattern not in get_filter_settings(get_config())[list_name]:
        warning(
            f"'{pattern}' saved globally but absent from the effective "
            f"{list_name} list — {_OVERRIDE_HINT}",
        )


def _list_remove(list_name: str, host: str) -> None:
    try:
        pattern = normalize_pattern(host)
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    removed = False

    def mutate(filter_cfg: dict[str, Any]) -> None:
        nonlocal removed
        entries = filter_cfg.setdefault(list_name, [])
        if not isinstance(entries, list):
            return
        kept = [e for e in entries if _normalized(e) != pattern]
        if len(kept) != len(entries):
            filter_cfg[list_name] = kept
            removed = True

    update_global_filter(mutate)
    if not removed:
        warning(f"'{pattern}' is not in the {list_name} list")
        return
    _sync_filter_file()
    success(f"Removed '{pattern}' from the {list_name} list")
    if pattern in get_filter_settings(get_config())[list_name]:
        warning(
            f"'{pattern}' removed globally but still in the effective "
            f"{list_name} list — {_OVERRIDE_HINT}",
        )


@allow_app.command("add")
def allow_add(host: Annotated[str, typer.Argument(help="Host pattern")]) -> None:
    """Add a host pattern to the allow list."""
    _list_add("allow", host)


@allow_app.command("remove")
def allow_remove(host: Annotated[str, typer.Argument(help="Host pattern")]) -> None:
    """Remove a host pattern from the allow list."""
    _list_remove("allow", host)


@deny_app.command("add")
def deny_add(host: Annotated[str, typer.Argument(help="Host pattern")]) -> None:
    """Add a host pattern to the deny list."""
    _list_add("deny", host)


@deny_app.command("remove")
def deny_remove(host: Annotated[str, typer.Argument(help="Host pattern")]) -> None:
    """Remove a host pattern from the deny list."""
    _list_remove("deny", host)


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

    auto_clean = bool(config.get("auto_clean", True))
    updated = False
    if _is_latest_tag(proxy_image):
        info("Checking for proxy image updates…")
        updated = manager.pull_if_newer(proxy_image)
        if updated:
            info("New image available — restarting proxy")
            existing = manager.find_proxy()
            if existing:
                existing.remove(force=True)

    # Materialize filter rules so the proxy picks them up (and hand-edits to
    # config.yaml are synced on start); write_filter_file warns on an invalid
    # configured mode.
    write_filter_file(config)

    info("Starting proxy")
    manager.ensure_proxy(
        image=proxy_image,
        db_path=db_path,
        ca_dir=ca_dir,
        network=network_name,
    )
    if auto_clean:
        # Swept last: the replaced image is only removable once the proxy
        # container that held it has been recreated on the new one.
        manager.clean_untagged_images()
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
