"""VibePod Dash integration.

`vp run` wires the container so the agent shows up on a
[vibepod-dash](https://github.com/VibePod/vibepod-dash) board: ``VPDASH_*`` env
is injected, the vendored reporter and hook scripts are copied into the agent's
config dir, and the CLI itself reports the container's start and stop — so
agents without lifecycle hooks still appear on the board with a state.

Everything soft-fails: an unreachable or misconfigured dashboard never blocks
a run.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on Python 3.10 CI
    import tomli as tomllib

from vibepod.core.hooksync import sync_integration_files
from vibepod.utils.console import info, warning

ENV_URL = "VPDASH_URL"
ENV_TOKEN = "VPDASH_TOKEN"
ENV_CONTAINER_URL = "VPDASH_CONTAINER_URL"
#: Every VibePod container gets host.docker.internal mapped to the host gateway
#: (see core/docker.py), so a dashboard on the host is reachable under it even
#: though `localhost` inside the container is the container itself.
CONTAINER_HOST = "host.docker.internal"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"})
#: Hook trace log, written inside the agent config dir (host-visible via the
#: config mount) and read back by `vp doctor dash`.
HOOK_LOG_NAME = "dash-hook.log"
#: Container labels carrying the dashboard identity of a run, so `vp stop` can
#: mark the agent finished from any terminal.
AGENT_ID_LABEL = "vibepod.dash.agent-id"
AGENT_LABEL = "vibepod.dash.agent"
REQUEST_TIMEOUT = 3

#: agent -> list of (path relative to resources/dash, dest relative to the
#: agent config dir). Dests follow each agent's in-container home layout, the
#: same way the herdr integration does.
BUILTIN_INTEGRATIONS: dict[str, list[tuple[str, str]]] = {
    "claude": [
        ("vpdash-report.sh", "hooks/vpdash-report.sh"),
        ("claude/dash-agent-state.sh", "hooks/dash-agent-state.sh"),
    ],
    "codex": [
        ("vpdash-report.sh", ".codex/vpdash-report.sh"),
        ("codex/dash-agent-state.sh", ".codex/dash-agent-state.sh"),
    ],
    "copilot": [
        ("vpdash-report.sh", ".copilot/hooks/vpdash-report.sh"),
        ("copilot/dash-agent-state.sh", ".copilot/hooks/dash-agent-state.sh"),
    ],
}


@dataclass(frozen=True)
class DashTarget:
    """A dashboard, as seen from both sides of the container boundary."""

    #: URL the CLI itself posts to.
    host_url: str
    #: The same dashboard, addressed from inside the container.
    container_url: str
    token: str | None
    agent: str
    agent_id: str
    name: str


def _dash_section(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("dash")
    return value if isinstance(value, dict) else {}


def dash_enabled(config: dict[str, Any]) -> bool:
    """Config gate: ``dash: false`` or ``dash: {enabled: false}`` disables."""
    value = config.get("dash", True)
    if isinstance(value, dict):
        return bool(value.get("enabled", True))
    return bool(value)


def resolve_url(config: dict[str, Any]) -> str | None:
    """Dashboard URL from the environment, else config ``dash.url``."""
    raw = os.environ.get(ENV_URL) or str(_dash_section(config).get("url", "") or "")
    raw = raw.strip().rstrip("/")
    if not raw:
        return None
    if "://" not in raw:
        raw = f"http://{raw}"
    return raw


def resolve_token(config: dict[str, Any]) -> str | None:
    """Ingest token from the environment, else config ``dash.token``."""
    raw = os.environ.get(ENV_TOKEN) or str(_dash_section(config).get("token", "") or "")
    return raw.strip() or None


def container_url(url: str) -> str:
    """Rewrite a loopback dashboard URL to one the container can reach.

    ``http://localhost:8765`` on the host is the container itself from inside,
    which is the single most common way to wire this up wrong.
    """
    parts = urllib.parse.urlsplit(url)
    if (parts.hostname or "").lower() not in LOOPBACK_HOSTS:
        return url
    netloc = CONTAINER_HOST if parts.port is None else f"{CONTAINER_HOST}:{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def resolve_container_url(config: dict[str, Any], host_url: str) -> str:
    """The dashboard URL to hand the container.

    Defaults to the loopback rewrite above, which suits a dash server whose
    port is published on the host. Set ``dash.container_url`` (or
    ``VPDASH_CONTAINER_URL``) when the container reaches it some other way —
    e.g. ``http://vibepod-dash:8765`` when the dash container joins the
    VibePod network, where the host name would not resolve for the CLI's own
    reports.
    """
    raw = os.environ.get(ENV_CONTAINER_URL) or str(
        _dash_section(config).get("container_url", "") or "",
    )
    raw = raw.strip().rstrip("/")
    if not raw:
        return container_url(host_url)
    return raw if "://" in raw else f"http://{raw}"


def agent_id(agent: str, workspace: Path, host: str) -> str:
    """Stable dashboard id for *agent* working in *workspace* on *host*.

    Deterministic on purpose: re-running an agent in the same checkout updates
    the card it had before instead of stacking up a new one every session.
    Override with ``VPDASH_AGENT_ID`` when you want one card per run.
    """
    seed = f"{host}|{agent}|{workspace}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def display_name(agent: str, workspace: Path) -> str:
    """Card title; the ``vp:`` prefix marks a VibePod-run agent, as herdr does."""
    return f"vp:{agent} · {workspace.name or workspace}"


def make_target(agent: str, workspace: Path, config: dict[str, Any]) -> DashTarget | None:
    """Build the target for this run, or None when no dashboard is configured."""
    url = resolve_url(config)
    if url is None:
        return None
    host = os.environ.get("VPDASH_HOST") or socket.gethostname()
    return DashTarget(
        host_url=url,
        container_url=resolve_container_url(config, url),
        token=resolve_token(config),
        agent=agent,
        agent_id=os.environ.get("VPDASH_AGENT_ID") or agent_id(agent, workspace, host),
        name=os.environ.get("VPDASH_AGENT_NAME") or display_name(agent, workspace),
    )


def container_env(target: DashTarget, config_mount_path: str) -> dict[str, str]:
    """Environment the in-container hooks need to report to *target*."""
    env = {
        ENV_URL: target.container_url,
        "VPDASH_AGENT": target.agent,
        "VPDASH_AGENT_ID": target.agent_id,
        "VPDASH_AGENT_NAME": target.name,
        "VPDASH_HOST": os.environ.get("VPDASH_HOST") or socket.gethostname(),
        "VPDASH_LOG": f"{config_mount_path.rstrip('/')}/{HOOK_LOG_NAME}",
    }
    if target.token:
        env[ENV_TOKEN] = target.token
    return env


def resource_root() -> Path:
    """Root of the vendored dash client files inside the package."""
    return Path(str(importlib.resources.files("vibepod"))) / "resources" / "dash"


def sync_dash_files(agent: str, config_dir: Path, config: dict[str, Any]) -> int:
    """Copy dash integration files for *agent* into its config dir.

    Built-in vendored files first, then user entries from config
    ``dash.integrations.<agent>`` (list of {source, dest}).
    """
    entries = _dash_section(config).get("integrations", {})
    custom = entries.get(agent, []) if isinstance(entries, dict) else []
    return sync_integration_files(
        label="dash",
        root=resource_root(),
        builtin=BUILTIN_INTEGRATIONS.get(agent, []),
        config_dir=config_dir,
        custom=custom or [],
        agent=agent,
    )


_DASH_MARKER = "dash-agent-state.sh"
_CLAUDE_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Notification",
    "Stop",
    "SessionEnd",
)
_CODEX_NOTIFY_LINE = 'notify = ["/config/.codex/dash-agent-state.sh"]'


def _claude_hook_entry() -> dict[str, Any]:
    return {
        "hooks": [
            {"type": "command", "command": '"$CLAUDE_CONFIG_DIR"/hooks/dash-agent-state.sh'},
        ],
    }


def register_claude_hooks(config_dir: Path) -> None:
    """Merge the dash hook into claude's settings.json, idempotently."""
    settings_path = config_dir / "settings.json"
    settings: dict[str, Any] = {}
    if settings_path.is_file():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            warning("dash: could not parse claude settings.json, skipping hook registration")
            return
        if isinstance(loaded, dict):
            settings = loaded
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        warning("dash: claude settings.json 'hooks' is not an object, skipping")
        return
    changed = False
    for event in _CLAUDE_EVENTS:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            warning(f"dash: claude settings.json hooks['{event}'] is not a list, skipping")
            continue
        present = any(
            _DASH_MARKER in hook.get("command", "")
            for item in entries
            if isinstance(item, dict)
            for hook in item.get("hooks", [])
            if isinstance(hook, dict)
        )
        if not present:
            entries.append(_claude_hook_entry())
            changed = True
    if changed:
        settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def register_codex_notify(config_dir: Path) -> None:
    """Point codex's notify program at the dash hook when nothing else claims it.

    Codex allows exactly one notify program, so an existing one (herdr's, or
    the user's) is left alone — the dashboard then relies on the CLI-side
    start/stop reports instead of turn-level events. The line must live in the
    TOML root table, so it goes at the top of the file.
    """
    config_path = config_dir / ".codex" / "config.toml"
    content = ""
    if config_path.is_file():
        try:
            content = config_path.read_text(encoding="utf-8")
        except OSError:
            warning("dash: could not read codex config.toml, skipping notify registration")
            return
    try:
        parsed = tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        warning("dash: codex config.toml is not valid TOML, skipping notify registration")
        return
    if "notify" in parsed:
        if _CODEX_NOTIFY_LINE not in content:
            warning("dash: codex config.toml already sets 'notify', leaving it untouched")
        return

    new_content = _CODEX_NOTIFY_LINE + "\n" + content
    try:
        tomllib.loads(new_content)
    except tomllib.TOMLDecodeError:
        warning("dash: notify registration would corrupt codex config.toml, skipping")
        return
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(new_content, encoding="utf-8")


def report(
    target: DashTarget,
    state: str,
    *,
    event: str | None = None,
    message: str | None = None,
    cwd: Path | str | None = None,
    quiet: bool = False,
) -> bool:
    """POST one state report to the dashboard. Never raises."""
    payload: dict[str, str] = {
        "agent": target.agent,
        "agent_id": target.agent_id,
        "state": state,
        "host": os.environ.get("VPDASH_HOST") or socket.gethostname(),
    }
    if target.name:
        payload["name"] = target.name
    if event:
        payload["event"] = event
    if message:
        payload["message"] = message
    if cwd:
        payload["cwd"] = str(cwd)

    headers = {"Content-Type": "application/json"}
    if target.token:
        headers["Authorization"] = f"Bearer {target.token}"
    request = urllib.request.Request(
        f"{target.host_url}/api/v1/events",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT):
            return True
    except (urllib.error.URLError, OSError, ValueError) as exc:
        if not quiet:
            warning(f"dash: could not report '{state}' to {target.host_url}: {exc}")
        return False


def apply_dash_if_enabled(
    agent: str,
    config_dir: Path,
    workspace: Path,
    config: dict[str, Any],
    *,
    config_mount_path: str,
    no_dash: bool,
) -> tuple[DashTarget | None, dict[str, str]]:
    """Wire the dash integration for this run. Never raises.

    Returns ``(target, env)``; both empty when no dashboard is configured, the
    config disables it, or ``--no-dash`` was passed.
    """
    if no_dash or not dash_enabled(config):
        return None, {}
    target = make_target(agent, workspace, config)
    if target is None:
        return None, {}
    try:
        synced = sync_dash_files(agent, config_dir, config)
        if agent == "claude":
            register_claude_hooks(config_dir)
        elif agent == "codex":
            register_codex_notify(config_dir)
    except Exception as exc:  # noqa: BLE001 - dash problems must never block a run
        warning(f"dash: could not prepare integration files: {exc}")
        return target, container_env(target, config_mount_path)
    detail = f"{synced} hook file(s)" if synced else "start/stop reports only"
    info(f"dash: reporting {target.name} to {target.host_url} ({detail})")
    return target, container_env(target, config_mount_path)


def target_from_labels(labels: dict[str, str], config: dict[str, Any]) -> DashTarget | None:
    """Rebuild a target from a container's labels, for `vp stop`.

    The name is left empty: the dashboard keeps the one it already has rather
    than being renamed by a bare stop report.
    """
    agent = labels.get(AGENT_LABEL)
    dash_agent_id = labels.get(AGENT_ID_LABEL)
    if not agent or not dash_agent_id:
        return None
    url = resolve_url(config)
    if url is None or not dash_enabled(config):
        return None
    return DashTarget(
        host_url=url,
        container_url=resolve_container_url(config, url),
        token=resolve_token(config),
        agent=agent,
        agent_id=dash_agent_id,
        name="",
    )
