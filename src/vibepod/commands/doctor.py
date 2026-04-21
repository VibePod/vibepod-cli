"""Doctor subcommands — inspect agent auth state on the host."""

from __future__ import annotations

import json
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

from vibepod.core.agents import agent_config_dir
from vibepod.utils.console import console, error, success, warning

app = typer.Typer(help="Inspect agent auth and config state")


def _format_mtime(path: Path) -> str:
    try:
        ts = path.stat().st_mtime
    except OSError:
        return "unknown"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
    age = time.time() - ts
    if age < 60:
        age_str = f"{int(age)}s ago"
    elif age < 3600:
        age_str = f"{int(age // 60)}m ago"
    elif age < 86400:
        age_str = f"{int(age // 3600)}h ago"
    else:
        age_str = f"{int(age // 86400)}d ago"
    return f"{dt.strftime('%Y-%m-%d %H:%M:%S %z')} ({age_str})"


def _format_expiry(expires_at_ms: int) -> tuple[str, bool]:
    """Return (human string, is_expired)."""
    now_ms = int(time.time() * 1000)
    delta_ms = expires_at_ms - now_ms
    dt = datetime.fromtimestamp(expires_at_ms / 1000, tz=timezone.utc).astimezone()
    when = dt.strftime("%Y-%m-%d %H:%M:%S %z")
    if delta_ms <= 0:
        return f"{when} (EXPIRED {abs(delta_ms) // 60000}m ago)", True
    minutes = delta_ms // 60000
    if minutes < 60:
        rel = f"in {minutes}m"
    elif minutes < 1440:
        rel = f"in {minutes // 60}h {minutes % 60}m"
    else:
        rel = f"in {minutes // 1440}d {(minutes % 1440) // 60}h"
    return f"{when} ({rel})", False


def _file_ownership(path: Path) -> str:
    try:
        st = path.stat()
    except OSError as exc:
        return f"<stat error: {exc}>"
    mode = stat.S_IMODE(st.st_mode)
    return f"uid={st.st_uid} gid={st.st_gid} mode={oct(mode)}"


@app.command("claude")
def claude() -> None:
    """Inspect Claude Code credential state for diagnosing auth/refresh issues."""
    cfg_dir = agent_config_dir("claude")
    console.print(f"[bold]Claude config dir:[/bold] {cfg_dir}")

    if not cfg_dir.exists():
        error(f"Config dir does not exist: {cfg_dir}")
        raise typer.Exit(1)

    creds_path = cfg_dir / ".credentials.json"
    claude_json = cfg_dir / ".claude.json"
    creds_expired = False

    console.print()
    console.print("[bold].credentials.json[/bold]")
    if not creds_path.exists():
        warning(f"  missing: {creds_path}")
        warning("  → run `/login` inside the container to create credentials")
    else:
        console.print(f"  path:       {creds_path}")
        console.print(f"  ownership:  {_file_ownership(creds_path)}")
        console.print(f"  modified:   {_format_mtime(creds_path)}")

        try:
            data: dict[str, Any] = json.loads(creds_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            error(f"  could not parse credentials: {exc}")
            raise typer.Exit(1) from exc

        oauth = data.get("claudeAiOauth") or {}
        if not oauth:
            warning("  no 'claudeAiOauth' block — file may use a different auth scheme")
        else:
            access = oauth.get("accessToken")
            refresh = oauth.get("refreshToken")
            expires_at = oauth.get("expiresAt")
            scopes = oauth.get("scopes") or oauth.get("scope")
            subscription = oauth.get("subscriptionType") or oauth.get("subscription")

            console.print(f"  accessToken:   {'present' if access else 'MISSING'}")
            console.print(f"  refreshToken:  {'present' if refresh else 'MISSING'}")
            if scopes:
                console.print(f"  scopes:        {scopes}")
            if subscription:
                console.print(f"  subscription:  {subscription}")

            if isinstance(expires_at, (int, float)):
                pretty, creds_expired = _format_expiry(int(expires_at))
                label = "[red]" if creds_expired else "[green]"
                console.print(f"  expiresAt:     {label}{pretty}[/]")
            else:
                warning("  expiresAt missing or not numeric")

            if not refresh:
                warning(
                    "  → no refreshToken present; Claude Code cannot rotate this "
                    "session and will require re-login after expiry"
                )

    console.print()
    console.print("[bold].claude.json[/bold]")
    if claude_json.exists():
        console.print(f"  ownership:  {_file_ownership(claude_json)}")
        console.print(f"  modified:   {_format_mtime(claude_json)}")
    else:
        console.print("  not present")

    console.print()
    console.print("[bold]Stored long-lived token[/bold]")
    stored_path = cfg_dir / "oauth-token"
    stored_token_present = False
    if stored_path.exists():
        try:
            stored_value = stored_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            warning(f"  could not read: {exc}")
            stored_value = ""
        if stored_value:
            stored_token_present = True
            console.print(f"  path:       {stored_path}")
            console.print(f"  ownership:  {_file_ownership(stored_path)}")
            console.print(f"  modified:   {_format_mtime(stored_path)}")
            console.print(f"  length:     {len(stored_value)} chars")
        else:
            console.print(f"  {stored_path} is empty")
    else:
        console.print("  not present — run `vp run claude setup-token` to create one")

    console.print()
    console.print("[bold]Host environment overrides[/bold]")
    found_any = False
    for key in (
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
    ):
        value = os.environ.get(key)
        if value:
            found_any = True
            masked = f"set (len={len(value)})" if "KEY" in key or "TOKEN" in key else value
            console.print(f"  {key}: {masked}")
    if not found_any:
        console.print("  none set on host")
    console.print(
        "  [dim]note: these are host-side; the container sees its own env.[/dim]"
    )

    console.print()
    console.print("[bold]Effective auth mode on next `vp run claude`[/bold]")
    if os.environ.get("ANTHROPIC_API_KEY"):
        console.print("  [green]ANTHROPIC_API_KEY[/green] (passed from host env)")
    elif os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        console.print("  [green]CLAUDE_CODE_OAUTH_TOKEN[/green] (passed from host env)")
    elif stored_token_present:
        console.print("  [green]stored long-lived token[/green] (no refresh needed)")
    elif creds_path.exists():
        console.print(
            "  [yellow]OAuth credentials.json[/yellow] "
            "(subject to the known refresh bug — may require /login when expired)"
        )
    else:
        console.print(
            "  [red]no auth[/red] — run `vp run claude` and `/login`, "
            "or `vp run claude setup-token`"
        )

    console.print()
    console.print("[bold]Tips[/bold]")
    console.print(
        "  • If `modified` on .credentials.json never updates past the original /login time,"
    )
    console.print("    the token is not being rotated. Re-run with:")
    console.print(
        "      [cyan]vp run claude -e ANTHROPIC_LOG=debug -e DEBUG=1[/cyan]"
    )
    console.print(
        "    and look for [dim][API:auth][/dim] entries near/after expiry to confirm."
    )
    console.print(
        "  • For headless/CI, consider `claude setup-token` + "
        "`-e CLAUDE_CODE_OAUTH_TOKEN=...` to bypass refresh entirely."
    )

    # Exit 2 only if credentials.json is expired AND nothing else would auth:
    # no env override, no stored token. If a stored token is present, the
    # expired OAuth file doesn't matter for the next run.
    effective_auth_broken = (
        creds_expired
        and not stored_token_present
        and not os.environ.get("ANTHROPIC_API_KEY")
        and not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    )
    if effective_auth_broken:
        raise typer.Exit(2)

    success("doctor check complete")
