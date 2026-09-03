"""Shared Codex lifecycle-hook registration for VibePod integrations."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on Python 3.10 CI
    import tomli as tomllib

from vibepod.utils.console import warning

LIFECYCLE_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "Stop",
    "Interrupt",
    "SessionEnd",
)

LEGACY_NOTIFY_LINES = frozenset(
    {
        'notify = ["/config/.codex/herdr-agent-state.sh"]',
        'notify = ["/config/.codex/dash-agent-state.sh"]',
    },
)


def _entry(command: str) -> dict[str, Any]:
    return {"hooks": [{"type": "command", "command": command}]}


def _commands(data: object) -> list[str]:
    if not isinstance(data, dict):
        return []
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return []
    return [
        command
        for groups in hooks.values()
        if isinstance(groups, list)
        for group in groups
        if isinstance(group, dict)
        for hook in group.get("hooks", [])
        if isinstance(hook, dict)
        and hook.get("type") == "command"
        and isinstance(command := hook.get("command"), str)
    ]


def _remove_legacy_notify(config_dir: Path, *, label: str) -> None:
    config_path = config_dir / ".codex" / "config.toml"
    if not config_path.is_file():
        return
    try:
        content = config_path.read_text(encoding="utf-8")
        tomllib.loads(content)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        warning(f"{label}: codex config.toml is not valid TOML; legacy notify left untouched")
        return

    lines = content.splitlines()
    retained = [line for line in lines if line.strip() not in LEGACY_NOTIFY_LINES]
    if retained == lines:
        return
    new_content = "\n".join(retained) + ("\n" if content.endswith("\n") and retained else "")
    try:
        config_path.write_text(new_content, encoding="utf-8")
    except OSError as exc:
        warning(f"{label}: could not remove legacy codex notify: {exc}")


def register(config_dir: Path, command: str, *, label: str) -> bool:
    """Merge one VibePod command into all Codex lifecycle events."""
    path = config_dir / ".codex" / "hooks.json"
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            warning(f"{label}: could not parse codex hooks.json, skipping hook registration")
            return False
        if not isinstance(loaded, dict):
            warning(f"{label}: codex hooks.json is not an object, skipping hook registration")
            return False
        data = loaded

    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        warning(f"{label}: codex hooks.json 'hooks' is not an object, skipping registration")
        return False

    changed = False
    for event in LIFECYCLE_EVENTS:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            warning(f"{label}: codex hooks.json hooks['{event}'] is not a list, skipping")
            continue
        present = any(
            hook.get("type") == "command" and hook.get("command") == command
            for group in groups
            if isinstance(group, dict)
            for hook in group.get("hooks", [])
            if isinstance(hook, dict)
        )
        if not present:
            groups.append(_entry(command))
            changed = True

    if changed:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            warning(f"{label}: could not write codex hooks.json: {exc}")
            return False

    _remove_legacy_notify(config_dir, label=label)
    return True


def registered(config_dir: Path, command: str) -> bool:
    """Return whether *command* appears in the Codex lifecycle hook file."""
    path = config_dir / ".codex" / "hooks.json"
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return command in _commands(data)
