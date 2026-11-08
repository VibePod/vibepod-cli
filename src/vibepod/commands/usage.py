"""Local subscription quotas. Provider credentials stay with the official clients."""

from __future__ import annotations

import json
import time
import webbrowser
from typing import Annotated

import typer
from rich.table import Table

from vibepod.core.config import get_config
from vibepod.core.profiles import resolve_profile
from vibepod.core.subscription_usage import PROVIDERS, UsageStore, refresh, setup_claude
from vibepod.utils.console import console, error, info, success

app = typer.Typer(help="Read subscription quotas without exporting provider credentials")


def _profile(value: str | None) -> str:
    try:
        return resolve_profile(value, get_config())
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(2) from exc


def _providers(value: str) -> tuple[str, ...]:
    if value == "antigravity":
        value = "agy"
    if value == "all":
        return PROVIDERS
    if value not in PROVIDERS:
        error("Choose codex, claude, agy (antigravity), or all.")
        raise typer.Exit(2)
    return (value,)


@app.callback(invoke_without_command=True)
def usage(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        show(False)


@app.command()
def show(
    as_json: Annotated[bool, typer.Option("--json", help="Print sanitized JSON")] = False,
) -> None:
    """Show the last snapshots; makes no provider or Docker requests."""
    snapshot = UsageStore().snapshot()
    if as_json:
        print(json.dumps(snapshot, indent=2))
        return
    table = Table(title="Subscription usage")
    for name in ("Provider", "Profile", "Limit", "Remaining", "State"):
        table.add_column(name)
    for row in snapshot["providers"]:
        for window in row["windows"] or [{}]:
            used = window.get("used_percent")
            remaining = f"{100 - used:.1f}%" if used is not None else "Unknown"
            if window.get("disabled"):
                remaining = "Unavailable"
            state = row["error"] or ("stale" if window.get("stale", row["stale"]) else "observed")
            table.add_row(
                row["provider"], row["profile"], window.get("bucket", "—"), remaining, state
            )
    console.print(table)
    if not snapshot["providers"]:
        info("No snapshots yet. Run vp usage refresh with your agents running.")


@app.command("refresh")
def refresh_usage(
    provider: Annotated[str, typer.Option(help="codex, claude, agy, or all")] = "all",
    profile: Annotated[str | None, typer.Option(help="VibePod credential profile")] = None,
    container: Annotated[str | None, typer.Option(help="Select a running agent container")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print sanitized JSON")] = False,
) -> None:
    """Read Codex/Antigravity quotas and the last passive Claude snapshot."""
    providers = _providers(provider)
    selected = _profile(profile)
    refresh(selected, providers, container)
    show(as_json)
    rows = [
        r
        for r in UsageStore().snapshot()["providers"]
        if r["profile"] == selected and r["provider"] in providers
    ]
    if any(r["error"] for r in rows):
        raise typer.Exit(1)


@app.command()
def watch(
    provider: Annotated[str, typer.Option(help="codex, claude, agy, or all")] = "all",
    profile: Annotated[str | None, typer.Option(help="VibePod credential profile")] = None,
    container: Annotated[str | None, typer.Option(help="Select a running agent container")] = None,
    interval: Annotated[int, typer.Option(min=60, help="Seconds between checks")] = 300,
) -> None:
    """Collect in the foreground until Ctrl-C; never starts an agent or a login."""
    selected, providers = _profile(profile), _providers(provider)
    info("Collecting usage locally. Press Ctrl-C to stop.")
    try:
        while True:
            refresh(selected, providers, container)
            time.sleep(interval)
    except KeyboardInterrupt:
        pass


@app.command("setup-claude")
def setup_claude_usage(
    profile: Annotated[str | None, typer.Option(help="VibePod credential profile")] = None,
    disable: Annotated[bool, typer.Option(help="Restore the original Claude status line")] = False,
) -> None:
    """Enable passive Claude snapshots, preserving the existing status-line command."""
    try:
        setup_claude(_profile(profile), disable=disable)
    except (OSError, ValueError):
        error("Could not update Claude settings; check their JSON and file permissions.")
        raise typer.Exit(1) from None
    success(
        "Claude quota capture disabled."
        if disable
        else "Claude quota capture enabled. Use vp run claude normally; "
        "no extra model calls are made."
    )


@app.command()
def dashboard(
    port: Annotated[
        int, typer.Option(min=1024, max=65535, help="Loopback-only dashboard port")
    ] = 8002,
    no_open: Annotated[bool, typer.Option(help="Do not open the browser")] = False,
) -> None:
    """Serve only sanitized quota snapshots; run vp usage watch separately to collect."""
    from vibepod.core.usage_dashboard import make_server

    server = make_server(UsageStore(), port)
    url = f"http://127.0.0.1:{port}"
    info(f"Subscription dashboard: {url}")
    info("Run vp usage watch in another terminal for ongoing collection.")
    if not no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
