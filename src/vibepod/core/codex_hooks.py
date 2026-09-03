"""Shared Codex lifecycle-hook registration for VibePod integrations."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Literal

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

RegistrationStatus = Literal["missing", "malformed", "handler-absent", "registered"]


def _entry(command: str) -> dict[str, Any]:
    return {"hooks": [{"type": "command", "command": command}]}


def _commands(data: object, event: str) -> list[str]:
    if not isinstance(data, dict) or not isinstance(data.get("hooks"), dict):
        return []
    groups = data["hooks"].get(event)
    if not isinstance(groups, list):
        return []
    return [
        command
        for group in groups
        if isinstance(group, dict)
        for nested in [group.get("hooks")]
        if isinstance(nested, list)
        for hook in nested
        if isinstance(hook, dict)
        and hook.get("type") == "command"
        and isinstance(command := hook.get("command"), str)
    ]


def _shape_error(hooks: dict[str, Any]) -> str | None:
    """Describe the first invalid matcher-group shape, if any."""
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            return f"hooks[{event!r}] is not a list"
        for group_index, group in enumerate(groups):
            if not isinstance(group, dict):
                return f"hooks[{event!r}][{group_index}] is not an object"
            nested = group.get("hooks", [])
            if not isinstance(nested, list):
                return f"hooks[{event!r}][{group_index}].hooks is not a list"
            for hook_index, hook in enumerate(nested):
                if not isinstance(hook, dict):
                    return f"hooks[{event!r}][{group_index}].hooks[{hook_index}] is not an object"
    return None


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
    if shape_error := _shape_error(hooks):
        warning(f"{label}: codex hooks.json {shape_error}, skipping registration")
        return False

    changed = False
    for event in LIFECYCLE_EVENTS:
        groups = hooks.setdefault(event, [])
        present = any(
            hook.get("type") == "command" and hook.get("command") == command
            for group in groups
            for hook in group.get("hooks", [])
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


def registration_status(config_dir: Path, command: str) -> RegistrationStatus:
    """Inspect one command's complete lifecycle registration without warning."""
    path = config_dir / ".codex" / "hooks.json"
    if not path.is_file():
        return "missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "malformed"
    if not isinstance(data, dict):
        return "malformed"
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict) or _shape_error(hooks):
        return "malformed"
    if all(command in _commands(data, event) for event in LIFECYCLE_EVENTS):
        return "registered"
    return "handler-absent"


def registered(config_dir: Path, command: str) -> bool:
    """Return whether *command* is registered for every lifecycle event."""
    return registration_status(config_dir, command) == "registered"
