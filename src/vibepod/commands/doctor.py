"""Doctor subcommands — inspect agent auth state on the host."""

from __future__ import annotations

import json
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

import typer

from vibepod.core.agents import agent_config_dir
from vibepod.core.config import get_config
from vibepod.core.profiles import resolve_profile
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
def claude(
    profile: Annotated[
        str | None,
        typer.Option("--profile", help="Credential profile to inspect (see `vp profile list`)"),
    ] = None,
) -> None:
    """Inspect Claude Code credential state for diagnosing auth/refresh issues."""
    try:
        active_profile = resolve_profile(profile, get_config())
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc
    cfg_dir = agent_config_dir("claude", active_profile)
    console.print(f"[bold]Claude config dir:[/bold] {cfg_dir} (profile: {active_profile})")

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
                    "session and will require re-login after expiry",
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
        "  [dim]note: these are host-side; the container sees its own env.[/dim]",
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
            "(subject to the known refresh bug — may require /login when expired)",
        )
    else:
        console.print(
            "  [red]no auth[/red] — run `vp run claude` and `/login`, "
            "or `vp run claude setup-token`",
        )

    console.print()
    console.print("[bold]Tips[/bold]")
    console.print(
        "  • If `modified` on .credentials.json never updates past the original /login time,",
    )
    console.print("    the token is not being rotated. Re-run with:")
    console.print(
        "      [cyan]vp run claude -e ANTHROPIC_LOG=debug -e DEBUG=1[/cyan]",
    )
    console.print(
        "    and look for [dim][API:auth][/dim] entries near/after expiry to confirm.",
    )
    console.print(
        "  • For headless/CI, consider `claude setup-token` + "
        "`-e CLAUDE_CODE_OAUTH_TOKEN=...` to bypass refresh entirely.",
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


def _elf_linkage(path: Path) -> str:
    """Describe an executable: ELF interpreter (dynamic), static, or script."""
    import struct

    try:
        data = path.read_bytes()
    except OSError as exc:
        return f"unreadable ({exc})"
    if data[:2] == b"#!":
        newline = data.find(b"\n")
        shebang = (data[2:newline] if newline != -1 else data[2:]).decode(errors="replace").strip()
        return f"script: {shebang}"
    if data[:4] != b"\x7fELF":
        return "not an ELF executable"
    if len(data) < 0x40:
        return "truncated ELF header"
    is64 = data[4] == 2
    fmt, phoff_off, phentsize_off, phnum_off = (
        ("<Q", 0x20, 0x36, 0x38) if is64 else ("<I", 0x1C, 0x2A, 0x2C)
    )
    try:
        (phoff,) = struct.unpack_from(fmt, data, phoff_off)
        (phentsize,) = struct.unpack_from("<H", data, phentsize_off)
        (phnum,) = struct.unpack_from("<H", data, phnum_off)
        for i in range(phnum):
            base = phoff + i * phentsize
            (p_type,) = struct.unpack_from("<I", data, base)
            if p_type == 3:  # PT_INTERP
                if is64:
                    (offset,) = struct.unpack_from("<Q", data, base + 0x08)
                    (size,) = struct.unpack_from("<Q", data, base + 0x20)
                else:
                    (offset,) = struct.unpack_from("<I", data, base + 0x04)
                    (size,) = struct.unpack_from("<I", data, base + 0x10)
                interp = data[offset : offset + size].rstrip(b"\x00").decode(errors="replace")
                return f"dynamic, needs {interp}"
    except struct.error as exc:
        return f"malformed ELF ({exc})"
    return "static"


def _herdr_log_relpath(agent: str) -> str | None:
    """Config-dir-relative path of the hook trace log for sh-hook agents."""
    return {
        "claude": "herdr-hook.log",
        "codex": ".codex/herdr-hook.log",
        "copilot": ".copilot/herdr-hook.log",
    }.get(agent)


def _herdr_agent_summary(profile: str) -> None:
    """One line per supported agent: integration, injection, registration, activity."""
    from rich.table import Table

    from vibepod.constants import SUPPORTED_AGENTS
    from vibepod.core import herdr as herdr_core
    from vibepod.core.config import get_config

    config = get_config()
    herdr_cfg = config.get("herdr")
    custom = (herdr_cfg or {}).get("integrations", {}) if isinstance(herdr_cfg, dict) else {}

    table = Table(title="herdr integration per agent")
    for column in ("agent", "integration", "injected", "registration", "last activity"):
        table.add_column(column)

    for name in SUPPORTED_AGENTS:
        builtin = herdr_core.BUILTIN_INTEGRATIONS.get(name, [])
        custom_entries = custom.get(name) or []
        if builtin and custom_entries:
            integration = f"built-in +{len(custom_entries)} custom"
        elif builtin:
            integration = "built-in"
        elif custom_entries:
            integration = f"custom ({len(custom_entries)})"
        else:
            integration = "none"

        cfg_dir = agent_config_dir(name, profile)
        dests = [dest for _, dest in builtin] + [
            str(entry.get("dest")) for entry in custom_entries if isinstance(entry, dict)
        ]
        if not dests:
            injected = "n.a."
        else:
            present = sum(1 for dest in dests if (cfg_dir / dest).is_file())
            injected = "yes" if present == len(dests) else f"{present}/{len(dests)}"

        if name == "claude":
            settings = cfg_dir / "settings.json"
            ok = settings.is_file() and "herdr-agent-state.sh" in settings.read_text(
                encoding="utf-8",
                errors="replace",
            )
            registration = "settings.json" if ok else "MISSING"
        elif name == "codex":
            toml_path = cfg_dir / ".codex" / "config.toml"
            ok = toml_path.is_file() and "herdr-agent-state.sh" in toml_path.read_text(
                encoding="utf-8",
                errors="replace",
            )
            registration = "notify" if ok else "MISSING"
        elif dests:
            registration = "auto"
        else:
            registration = "-"

        activity = "-"
        log_rel = _herdr_log_relpath(name)
        if log_rel and (cfg_dir / log_rel).is_file():
            lines = (cfg_dir / log_rel).read_text(encoding="utf-8", errors="replace").splitlines()
            if lines:
                activity = lines[-1][:70]

        table.add_row(name, integration, injected, registration, activity)

    console.print(table)


@app.command("herdr")
def herdr_doctor(
    agent: Annotated[
        str | None,
        typer.Argument(help="Agent to inspect in depth; omit for an all-agents summary"),
    ] = None,
    profile: Annotated[
        str | None,
        typer.Option("--profile", help="Credential profile to inspect (see `vp profile list`)"),
    ] = None,
) -> None:
    """Diagnose herdr terminal-multiplexer wiring end to end.

    Run this INSIDE a herdr pane. Without an agent: passive summary of every
    agent's integration state. With an agent: checks pane env, socket,
    binary, the injected integration files, the hook trace log, performs a live
    host-side state report, and (when Docker is available) reproduces the
    exact in-container herdr call.
    """
    import subprocess

    from vibepod.constants import SUPPORTED_AGENTS
    from vibepod.core import herdr as herdr_core
    from vibepod.core.agents import effective_agent_image, get_agent_spec, resolve_agent_name
    from vibepod.core.config import get_config
    from vibepod.core.launch import host_user as _host_user

    if agent is not None:
        resolved = resolve_agent_name(agent)
        if resolved is None:
            error(f"Unknown agent '{agent}'. Supported: {', '.join(SUPPORTED_AGENTS)}")
            raise typer.Exit(1)
        agent = resolved

    try:
        active_profile = resolve_profile(profile, get_config())
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    failures = 0

    console.print("[bold]Pane environment[/bold]")
    for key in ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID"):
        value = os.environ.get(key)
        if value:
            console.print(f"  {key}: {value}")
        else:
            warning(f"  {key}: MISSING")
            if key in ("HERDR_ENV", "HERDR_PANE_ID"):
                failures += 1
    if os.environ.get("HERDR_ENV") != "1":
        warning("  → not inside a herdr pane; run `vp doctor herdr` from a pane")

    console.print()
    console.print("[bold]Socket[/bold]")
    sock = herdr_core.resolve_socket()
    if sock is None:
        error("  no herdr socket found (HERDR_SOCKET_PATH / ~/.config/herdr/herdr.sock)")
        failures += 1
    else:
        console.print(f"  path:      {sock}")
        console.print(f"  ownership: {_file_ownership(sock)}")

    console.print()
    console.print("[bold]Binary[/bold]")
    binary = herdr_core.resolve_binary()
    if binary is None:
        # not fatal: state reporting also works over the socket alone
        warning("  no herdr binary (HERDR_BIN_PATH unset and `herdr` not on PATH)")
    else:
        console.print(f"  path: {binary}")
        console.print(f"  linkage: {_elf_linkage(binary)}")
        try:
            proc = subprocess.run(
                [str(binary), "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            out = (proc.stdout or proc.stderr).strip()
            console.print(f"  version: rc={proc.returncode} {out}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            error(f"  could not execute binary: {exc}")
            failures += 1

    if agent is None:
        console.print()
        _herdr_agent_summary(active_profile)
        if failures:
            error(f"{failures} problem(s) found")
            raise typer.Exit(1)
        console.print()
        console.print("Deep-dive one agent with `vp doctor herdr <agent>`")
        return

    pane = os.environ.get("HERDR_PANE_ID", "")
    if binary is not None and pane:
        console.print()
        console.print("[bold]Host-side live report[/bold] (watch the sidebar)")
        cmd = [
            str(binary),
            "pane",
            "report-agent",
            pane,
            "--source",
            "vibepod:doctor",
            "--agent",
            agent,
            "--state",
            "working",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            out = (proc.stdout + proc.stderr).strip()
            status = success if proc.returncode == 0 else error
            status(f"  report-agent rc={proc.returncode}{' ' + out if out else ''}")
            if proc.returncode != 0:
                failures += 1
        except (OSError, subprocess.TimeoutExpired) as exc:
            error(f"  report failed: {exc}")
            failures += 1
        for probe in (["agent", "list"], ["pane", "get", pane]):
            try:
                proc = subprocess.run(
                    [str(binary), *probe],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                out = (proc.stdout + proc.stderr).strip()
                console.print(f"  herdr {' '.join(probe)} (rc={proc.returncode}):")
                for line in out.splitlines()[:15]:
                    console.print(f"    {line}")
            except (OSError, subprocess.TimeoutExpired) as exc:
                warning(f"  herdr {' '.join(probe)} failed: {exc}")

    console.print()
    console.print(f"[bold]Injected files ({agent})[/bold]")
    cfg_dir = agent_config_dir(agent, active_profile)
    entries = herdr_core.BUILTIN_INTEGRATIONS.get(agent, [])
    if not entries:
        console.print("  (no built-in integration for this agent)")
    for _resource, dest in entries:
        target = cfg_dir / dest
        if not target.is_file():
            warning(f"  missing: {target} — run `vp run {agent}` inside a pane to inject")
            failures += 1
        else:
            exec_ok = os.access(target, os.X_OK) if dest.endswith(".sh") else True
            console.print(f"  {target} ({'executable' if exec_ok else 'NOT EXECUTABLE'})")
            if not exec_ok:
                failures += 1
    if agent == "claude":
        settings = cfg_dir / "settings.json"
        registered = settings.is_file() and "herdr-agent-state.sh" in settings.read_text(
            encoding="utf-8",
            errors="replace",
        )
        (console.print if registered else warning)(
            f"  settings.json hooks: {'registered' if registered else 'NOT REGISTERED'}",
        )
        if not registered:
            failures += 1
    if agent == "codex":
        toml_path = cfg_dir / ".codex" / "config.toml"
        registered = toml_path.is_file() and "herdr-agent-state.sh" in toml_path.read_text(
            encoding="utf-8",
            errors="replace",
        )
        (console.print if registered else warning)(
            f"  config.toml notify: {'registered' if registered else 'NOT REGISTERED'}",
        )
        if not registered:
            failures += 1

    log_dirs = {"claude": "", "codex": ".codex", "copilot": ".copilot"}
    if agent in log_dirs:
        log_path = (
            cfg_dir / log_dirs[agent] / "herdr-hook.log"
            if log_dirs[agent]
            else cfg_dir / "herdr-hook.log"
        )
        console.print()
        console.print("[bold]Hook trace log[/bold]")
        if not log_path.is_file():
            warning(f"  {log_path} missing — hooks never fired in the container")
        else:
            console.print(f"  {log_path} (last 10 lines):")
            for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-10:]:
                console.print(f"    {line}")

    console.print()
    console.print("[bold]Container-side probe[/bold]")
    volumes, env = herdr_core.herdr_volumes_and_env()
    #: hook script dest + payload + shell line per sh-hook agent; the probe
    #: replays the exact in-container call path the agent itself would take.
    probe_payloads = {
        "claude": '{"hook_event_name":"Stop"}',
        "codex": '{"type":"agent-turn-complete"}',
        "copilot": '{"type":"stop"}',
    }
    probe_reported = False
    if not volumes or not pane or agent not in probe_payloads:
        warning("  skipped (needs socket + pane env; sh-hook agents only)")
    else:
        manager = None
        image = None
        binds: dict[str, dict[str, str]] = {}
        try:
            from vibepod.core.docker import DockerManager

            manager = DockerManager()
            config = get_config()
            image = effective_agent_image(agent, config)
            spec = get_agent_spec(agent)
            binds = {host: {"bind": dest, "mode": mode} for host, dest, mode in volumes}
            binds[str(cfg_dir)] = {"bind": spec.config_mount_path, "mode": "rw"}
            container_env = {
                **spec.extra_env,
                **env,
                **{
                    k: os.environ[k]
                    for k in ("HERDR_TAB_ID", "HERDR_WORKSPACE_ID")
                    if k in os.environ
                },
            }
            hook_dest = herdr_core.BUILTIN_INTEGRATIONS[agent][0][1]
            hook_path = f"{spec.config_mount_path}/{hook_dest}"
            payload = probe_payloads[agent]
            if agent == "codex":
                shell_line = f"{hook_path} '{payload}'"
            else:
                shell_line = f"printf '%s' '{payload}' | {hook_path}"
            output = manager.client.containers.run(
                image,
                entrypoint=["/bin/sh"],
                command=["-c", shell_line],
                volumes=binds,
                environment=container_env,
                user=_host_user(),
                network_mode="none",
                remove=True,
                stdout=True,
                stderr=True,
            )
            probe_reported = True
            text = (
                output.decode("utf-8", errors="replace").strip()
                if isinstance(output, bytes)
                else str(output).strip()
            )
            detail = f": {text}" if text else ""
            success(f"  in-container hook run finished{detail}")
            log_dir = {"claude": "", "codex": ".codex", "copilot": ".copilot"}[agent]
            probe_log = (
                cfg_dir / log_dir / "herdr-hook.log" if log_dir else cfg_dir / "herdr-hook.log"
            )
            if probe_log.is_file():
                lines = probe_log.read_text(encoding="utf-8", errors="replace").splitlines()
                console.print("  probe trace (newest log lines):")
                for line in lines[-2:]:
                    console.print(f"    {line}")
        except Exception as exc:  # noqa: BLE001 - diagnostic probe reports any failure verbatim
            error(f"  in-container probe failed: {exc}")
            failures += 1
            if manager is not None and image is not None:
                try:
                    inventory = manager.client.containers.run(
                        image,
                        entrypoint=["/bin/sh"],
                        command=[
                            "-c",
                            "ls -la /usr/local/bin/herdr 2>&1; "
                            "ls /lib64/ld-linux-* /lib/ld-linux-* /lib/ld-musl-* 2>&1; "
                            "command -v node ldd 2>&1",
                        ],
                        volumes=binds,
                        network_mode="none",
                        remove=True,
                        stdout=True,
                        stderr=True,
                    )
                    console.print("  image inventory (mounted file, loaders, node):")
                    text = (
                        inventory.decode("utf-8", errors="replace")
                        if isinstance(inventory, bytes)
                        else str(inventory)
                    )
                    for line in text.strip().splitlines():
                        console.print(f"    {line}")
                except Exception as inv_exc:  # noqa: BLE001
                    warning(f"  image inventory failed: {inv_exc}")

    if pane:
        console.print()
        sources = ["vibepod:doctor"]
        if probe_reported:
            # the container probe replays the real hook, which reports with
            # source "vibepod"; only then may doctor release that source
            sources.append("vibepod")
        released = []
        for source in sources:
            # legacy vp:-label too: older injected scripts reported it as the id
            for label in (agent, herdr_core.agent_label(agent)):
                if herdr_core.release_agent(agent, pane=pane, source=source, label=label):
                    released.append(f"{source}/{label}")
        if released:
            console.print(f"Released doctor-reported agent state ({', '.join(released)})")

    console.print()
    if failures:
        error(f"{failures} problem(s) found")
        raise typer.Exit(1)
    success(
        "herdr wiring looks healthy — if the sidebar stays empty, the reports reach "
        "herdr but it does not surface them; check `herdr agent list` output above",
    )
