"""Herdr terminal-multiplexer integration.

When ``vp run`` executes inside a herdr pane (https://herdr.dev), VibePod
wires the container so the agent shows up in herdr with live state:
the herdr unix socket and binary are bind-mounted in, ``HERDR_*`` env is
forwarded, and VibePod-authored hook scripts are injected into the agent's
config dir. Everything soft-fails: a broken herdr setup never blocks a run.
"""

from __future__ import annotations

import importlib.resources
import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on Python 3.10 CI
    import tomli as tomllib

from vibepod.utils.console import info, warning

DEFAULT_SOCKET = Path("~/.config/herdr/herdr.sock")
CONTAINER_SOCKET = "/herdr/herdr.sock"
CONTAINER_BINARY = "/usr/local/bin/herdr"
FORWARDED_ENV = ("HERDR_ENV", "HERDR_WORKSPACE_ID", "HERDR_TAB_ID", "HERDR_PANE_ID")

#: agent -> list of (path relative to resources/herdr, dest relative to the
#: agent config dir). Dests follow each agent's in-container home layout:
#: claude mounts the config dir at /claude (CLAUDE_CONFIG_DIR); the others
#: mount it at /config with HOME=/config, so dotted home paths apply.
BUILTIN_INTEGRATIONS: dict[str, list[tuple[str, str]]] = {
    "claude": [
        ("claude/hooks/herdr-agent-state.sh", "hooks/herdr-agent-state.sh"),
        ("herdr-report.js", "hooks/herdr-report.js"),
    ],
    "codex": [
        ("codex/herdr-agent-state.sh", ".codex/herdr-agent-state.sh"),
        ("herdr-report.js", ".codex/herdr-report.js"),
    ],
    "copilot": [
        ("copilot/hooks/herdr-agent-state.sh", ".copilot/hooks/herdr-agent-state.sh"),
        ("herdr-report.js", ".copilot/hooks/herdr-report.js"),
    ],
    "opencode": [
        ("opencode/plugins/herdr-agent-state.js", ".config/opencode/plugins/herdr-agent-state.js"),
    ],
    "pi": [("pi/extensions/herdr-agent-state.ts", ".pi/agent/extensions/herdr-agent-state.ts")],
    "tau": [("tau/extensions/herdr_agent_state.py", ".tau/extensions/herdr_agent_state.py")],
}


def resolve_socket() -> Path | None:
    """Return the herdr socket path when it exists, honouring HERDR_SOCKET_PATH."""
    raw = os.environ.get("HERDR_SOCKET_PATH")
    candidate = Path(raw).expanduser() if raw else DEFAULT_SOCKET.expanduser()
    # absolute: the path becomes a Docker bind-mount source, where a relative
    # string would be read as a named volume
    return candidate.absolute() if candidate.is_socket() else None


def resolve_binary() -> Path | None:
    """Return the host herdr binary, honouring HERDR_BIN_PATH over PATH lookup."""
    raw = os.environ.get("HERDR_BIN_PATH")
    if raw:
        candidate = Path(raw).expanduser()
        # absolute for the same bind-mount reason as resolve_socket
        return candidate.absolute() if candidate.is_file() else None
    found = shutil.which("herdr")
    return Path(found).absolute() if found else None


def herdr_active() -> bool:
    """True when running inside a herdr pane with a reachable socket."""
    return os.environ.get("HERDR_ENV") == "1" and resolve_socket() is not None


def herdr_volumes_and_env() -> tuple[list[tuple[str, str, str]], dict[str, str]]:
    """Return socket/binary bind mounts and container env for herdr wiring.

    Empty when the socket is unreachable. The binary mount is optional —
    hook scripts no-op when HERDR_BIN_PATH is unset in the container.
    """
    sock = resolve_socket()
    if sock is None:
        return [], {}
    volumes: list[tuple[str, str, str]] = [(str(sock), CONTAINER_SOCKET, "rw")]
    env: dict[str, str] = {"HERDR_SOCKET_PATH": CONTAINER_SOCKET}
    binary = resolve_binary()
    if binary is not None:
        volumes.append((str(binary), CONTAINER_BINARY, "ro"))
        env["HERDR_BIN_PATH"] = CONTAINER_BINARY
    for key in FORWARDED_ENV:
        value = os.environ.get(key)
        if value:
            env[key] = value
    return volumes, env


def resource_root() -> Path:
    """Root of the vendored herdr integration files inside the package."""
    return Path(str(importlib.resources.files("vibepod"))) / "resources" / "herdr"


def _copy_into(config_dir: Path, dest_rel: str, content: bytes, executable: bool) -> bool:
    dest = (config_dir / dest_rel).resolve()
    if config_dir.resolve() not in dest.parents:
        warning(f"herdr: destination '{dest_rel}' escapes the agent config dir, skipping")
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    if executable:
        dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return True


def sync_herdr_files(agent: str, config_dir: Path, config: dict[str, Any]) -> int:
    """Copy herdr integration files for *agent* into its config dir.

    Built-in vendored files first, then user entries from config
    ``herdr.integrations.<agent>`` (list of {source, dest}). Idempotent:
    VibePod-owned dests are overwritten each run; other files untouched.
    Returns the number of files synced.
    """
    synced = 0
    root = resource_root()
    for resource_rel, dest_rel in BUILTIN_INTEGRATIONS.get(agent, []):
        source = root / resource_rel
        if not source.is_file():
            warning(f"herdr: packaged resource missing: {resource_rel}")
            continue
        executable = resource_rel.endswith(".sh")
        if _copy_into(config_dir, dest_rel, source.read_bytes(), executable):
            synced += 1

    herdr_cfg = config.get("herdr")
    entries = (herdr_cfg or {}).get("integrations", {}) if isinstance(herdr_cfg, dict) else {}
    for entry in entries.get(agent, []) or []:
        if not isinstance(entry, dict) or "source" not in entry or "dest" not in entry:
            warning(f"herdr: invalid integration entry for '{agent}': {entry!r}")
            continue
        source = Path(str(entry["source"])).expanduser()
        if not source.is_file():
            warning(f"herdr: integration source not found: {source}")
            continue
        executable = os.access(source, os.X_OK)
        if _copy_into(config_dir, str(entry["dest"]), source.read_bytes(), executable):
            synced += 1
    return synced


_HERDR_MARKER = "herdr-agent-state.sh"
_CLAUDE_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Notification",
    "Stop",
    "SessionEnd",
)
_CODEX_NOTIFY_LINE = 'notify = ["/config/.codex/herdr-agent-state.sh"]'


def _claude_hook_entry() -> dict[str, Any]:
    return {
        "hooks": [
            {
                "type": "command",
                "command": '"$CLAUDE_CONFIG_DIR"/hooks/herdr-agent-state.sh',
            },
        ],
    }


def register_claude_hooks(config_dir: Path) -> None:
    """Merge herdr hook entries into the claude settings.json, idempotently."""
    settings_path = config_dir / "settings.json"
    settings: dict[str, Any] = {}
    if settings_path.is_file():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            warning("herdr: could not parse claude settings.json, skipping hook registration")
            return
        if isinstance(loaded, dict):
            settings = loaded
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        warning("herdr: claude settings.json 'hooks' is not an object, skipping")
        return
    changed = False
    for event in _CLAUDE_EVENTS:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            warning(f"herdr: claude settings.json hooks['{event}'] is not a list, skipping")
            continue
        present = any(
            _HERDR_MARKER in hook.get("command", "")
            for entry in entries
            if isinstance(entry, dict)
            for hook in entry.get("hooks", [])
            if isinstance(hook, dict)
        )
        if not present:
            entries.append(_claude_hook_entry())
            changed = True
    if changed:
        settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def register_codex_notify(config_dir: Path) -> None:
    """Point codex's notify program at our hook script, idempotently.

    The line must live in the TOML root table, so it is inserted at the top
    of the file — appending would place it inside the last ``[section]``.
    A previously misplaced line (early VibePod versions appended) is moved.
    """
    config_path = config_dir / ".codex" / "config.toml"
    content = ""
    if config_path.is_file():
        try:
            content = config_path.read_text(encoding="utf-8")
        except OSError:
            warning("herdr: could not read codex config.toml, skipping notify registration")
            return

    lines = content.splitlines()
    marker_at = next(
        (i for i, line in enumerate(lines) if line.strip() == _CODEX_NOTIFY_LINE),
        None,
    )
    if marker_at is not None:
        if not any(line.lstrip().startswith("[") for line in lines[:marker_at]):
            return
        del lines[marker_at]
        content = "\n".join(lines) + ("\n" if lines else "")

    try:
        parsed = tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        warning("herdr: codex config.toml is not valid TOML, skipping notify registration")
        return
    if "notify" in parsed:
        warning("herdr: codex config.toml already sets 'notify', leaving it untouched")
        return

    new_content = _CODEX_NOTIFY_LINE + "\n" + content
    try:
        tomllib.loads(new_content)
    except tomllib.TOMLDecodeError:
        warning("herdr: notify registration would corrupt codex config.toml, skipping")
        return
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(new_content, encoding="utf-8")


#: Container label carrying the herdr pane a run was started in, so
#: `vp stop` can release the agent entry from any terminal.
PANE_LABEL = "vibepod.herdr.pane"


def agent_label(agent: str) -> str:
    """Display name shown in the herdr sidebar; vp: signals a VibePod-run agent.

    Reports use the CANONICAL agent id in the `agent` field (herdr only
    renders recognized ids) and this value as `display_agent`.
    """
    return f"vp:{agent}"


def release_agent(
    agent: str,
    pane: str | None = None,
    source: str = "vibepod",
    label: str | None = None,
) -> bool:
    """Tell herdr the agent left the pane (``pane.release_agent``). Never raises.

    Herdr auto-clears only when the reporting process exits; the reports come
    from short-lived hook invocations inside the container, so the entry
    would otherwise outlive the agent. Sent over the socket directly.
    """
    import socket as socket_module

    pane = pane or os.environ.get("HERDR_PANE_ID")
    sock_path = resolve_socket()
    if not pane or sock_path is None:
        return False
    request = {
        "id": f"vibepod:{os.getpid()}:release",
        "method": "pane.release_agent",
        "params": {"pane_id": pane, "source": source, "agent": label or agent},
    }
    try:
        with socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM) as sock:
            sock.settimeout(3)
            sock.connect(str(sock_path))
            sock.sendall((json.dumps(request) + "\n").encode())
            reply = sock.recv(4096).decode("utf-8", errors="replace")
    except OSError as exc:
        warning(f"herdr: could not release agent state: {exc}")
        return False
    try:
        parsed = json.loads(reply.splitlines()[0]) if reply.strip() else {}
    except json.JSONDecodeError:
        return True
    if isinstance(parsed, dict) and parsed.get("error"):
        warning(f"herdr: release rejected: {parsed['error']}")
        return False
    return True


def reexec_with_agent_hint(agent: str, config: dict[str, Any], *, no_herdr: bool) -> None:
    """Re-exec vp with HERDR_AGENT in the STARTUP environment.

    Herdr reads the foreground process's /proc/<pid>/environ, which is a
    snapshot taken at exec time — setting os.environ later is invisible to
    it. The hint lets herdr use the named agent's screen manifest even
    though the pane runs `vp` (agents like codex have no working/blocked
    hook events, so screen detection is their only state source). No-op
    when the hint already matches (post-re-exec) or herdr is inactive.
    """
    if no_herdr or not herdr_enabled(config) or not herdr_active():
        return
    if os.environ.get("HERDR_AGENT") == agent or not sys.argv:
        return
    os.environ["HERDR_AGENT"] = agent
    try:
        os.execvp(sys.argv[0], sys.argv)
    except OSError as exc:
        warning(f"herdr: could not re-exec for the HERDR_AGENT hint: {exc}")


def _run_binary(args: list[str]) -> tuple[int, str]:
    """Run the host herdr CLI; (127, reason) when no binary is available."""
    import subprocess

    binary = resolve_binary()
    if binary is None:
        return 127, "no herdr binary"
    try:
        proc = subprocess.run([str(binary), *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _report_agent_via_socket(agent: str, pane: str) -> bool:
    """Report an idle agent with its display label over the host socket."""
    import socket as socket_module

    sock_path = resolve_socket()
    if sock_path is None:
        return False
    request = {
        "id": f"vibepod:{os.getpid()}:metadata",
        "method": "pane.report_agent",
        "params": {
            "pane_id": pane,
            "source": "vibepod",
            "agent": agent,
            "display_agent": agent_label(agent),
            "state": "idle",
        },
    }
    try:
        with socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM) as sock:
            sock.settimeout(3)
            sock.connect(str(sock_path))
            sock.sendall((json.dumps(request) + "\n").encode())
            reply = sock.recv(4096).decode("utf-8", errors="replace")
    except OSError:
        return False
    try:
        parsed = json.loads(reply.splitlines()[0]) if reply.strip() else {}
    except json.JSONDecodeError:
        return True
    if isinstance(parsed, dict) and parsed.get("error"):
        warning(f"herdr: metadata report rejected: {parsed['error']}")
        return False
    return True


def report_pane_metadata(agent: str) -> bool:
    """Show the vp:<agent> marker in the pane via socket and CLI metadata.

    The socket path carries ``display_agent`` on all Herdr versions. The CLI
    path additionally updates the pane title where supported; it falls back to
    title-only on older CLIs that reject ``--display-agent``.
    """
    pane = os.environ.get("HERDR_PANE_ID")
    if not pane:
        return False
    socket_ok = _report_agent_via_socket(agent, pane)
    binary = resolve_binary()
    if binary is None:
        return socket_ok

    label = agent_label(agent)
    base = ["pane", "report-metadata", pane, "--source", "vibepod", "--agent", agent]
    rc, out = _run_binary([*base, "--title", label, "--display-agent", label])
    if rc == 0:
        return True
    rc, out = _run_binary([*base, "--title", label])
    if rc == 0:
        return True
    if not socket_ok:
        warning(f"herdr: could not set pane metadata: {out}")
    return socket_ok


def clear_pane_metadata(agent: str) -> None:
    """Remove the vp:<agent> pane marker set by report_pane_metadata."""
    pane = os.environ.get("HERDR_PANE_ID")
    if not pane:
        return
    base = ["pane", "report-metadata", pane, "--source", "vibepod", "--agent", agent]
    rc, _ = _run_binary([*base, "--clear-title", "--clear-display-agent"])
    if rc != 0:
        _run_binary([*base, "--clear-title"])


def herdr_enabled(config: dict[str, Any]) -> bool:
    """Config gate: ``herdr: false`` or ``herdr: {enabled: false}`` disables."""
    value = config.get("herdr", True)
    if isinstance(value, dict):
        return bool(value.get("enabled", True))
    return bool(value)


def apply_herdr_if_enabled(
    agent: str,
    config_dir: Path,
    config: dict[str, Any],
    *,
    no_herdr: bool,
) -> tuple[list[tuple[str, str, str]], dict[str, str]]:
    """Wire herdr for this run when inside a herdr pane. Never raises.

    Returns (extra_volumes, env). Empty when disabled or not in a pane.
    """
    if no_herdr or not herdr_enabled(config) or not herdr_active():
        return [], {}
    volumes, env = herdr_volumes_and_env()
    if not volumes:
        return [], {}
    if "HERDR_BIN_PATH" not in env:
        warning(
            "no herdr binary found on the host; hooks will report via the "
            "socket directly (needs node in the agent image)",
        )
    try:
        synced = sync_herdr_files(agent, config_dir, config)
        if agent == "claude":
            register_claude_hooks(config_dir)
        elif agent == "codex":
            register_codex_notify(config_dir)
    except Exception as exc:  # noqa: BLE001 - herdr problems must never block a run
        warning(f"herdr: could not prepare integration files: {exc}")
        return volumes, env
    detail = f"{synced} integration file(s)" if synced else "socket and env only"
    info(f"herdr pane detected: wiring {agent} state reporting ({detail})")
    return volumes, env
