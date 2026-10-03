"""Shared Codex lifecycle-hook registration tests."""

from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


def _module() -> ModuleType:
    assert importlib.util.find_spec("vibepod.core.codex_hooks") is not None
    return importlib.import_module("vibepod.core.codex_hooks")


def _commands(path: Path, event: str) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        hook["command"]
        for group in data["hooks"][event]
        if isinstance(group, dict)
        for hook in group.get("hooks", [])
        if isinstance(hook, dict) and hook.get("type") == "command"
    ]


def test_register_creates_every_lifecycle_event(tmp_path: Path) -> None:
    codex_hooks = _module()
    command = "/config/.codex/dash-agent-state.sh"

    assert codex_hooks.register(tmp_path, command, label="dash")

    path = tmp_path / ".codex" / "hooks.json"
    for event in codex_hooks.LIFECYCLE_EVENTS:
        assert _commands(path, event) == [command]


def test_register_preserves_user_hooks_and_is_idempotent(tmp_path: Path) -> None:
    codex_hooks = _module()
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "description": "mine",
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "mine.sh"}]},
                    ],
                    "PreCompact": [
                        {"hooks": [{"type": "command", "command": "compact.sh"}]},
                    ],
                },
            },
        ),
        encoding="utf-8",
    )
    command = "/config/.codex/herdr-agent-state.sh"

    assert codex_hooks.register(tmp_path, command, label="herdr")
    first = path.read_text(encoding="utf-8")
    assert codex_hooks.register(tmp_path, command, label="herdr")

    assert path.read_text(encoding="utf-8") == first
    data = json.loads(first)
    assert data["description"] == "mine"
    assert _commands(path, "Stop") == ["mine.sh", command]
    assert _commands(path, "PreCompact") == ["compact.sh"]


def test_register_preserves_a_matcher_group_without_handlers(tmp_path: Path) -> None:
    codex_hooks = _module()
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash"}]}}),
        encoding="utf-8",
    )

    assert codex_hooks.register(tmp_path, "hook.sh", label="dash")

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["hooks"]["PreToolUse"][0] == {"matcher": "Bash"}
    assert _commands(path, "PreToolUse") == ["hook.sh"]


@pytest.mark.parametrize("order", [("herdr", "dash"), ("dash", "herdr")])
def test_dash_and_herdr_handlers_coexist(tmp_path: Path, order: tuple[str, str]) -> None:
    codex_hooks = _module()
    commands = {
        "dash": "/config/.codex/dash-agent-state.sh",
        "herdr": "/config/.codex/herdr-agent-state.sh",
    }

    for label in order:
        assert codex_hooks.register(tmp_path, commands[label], label=label)

    path = tmp_path / ".codex" / "hooks.json"
    for event in codex_hooks.LIFECYCLE_EVENTS:
        assert _commands(path, event) == [commands[label] for label in order]


@pytest.mark.parametrize(
    "legacy",
    [
        'notify = ["/config/.codex/herdr-agent-state.sh"]',
        'notify = ["/config/.codex/dash-agent-state.sh"]',
    ],
)
def test_register_removes_legacy_vibepod_notify(tmp_path: Path, legacy: str) -> None:
    codex_hooks = _module()
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(f'model = "gpt"\n{legacy}\n', encoding="utf-8")

    assert codex_hooks.register(
        tmp_path,
        "/config/.codex/dash-agent-state.sh",
        label="dash",
    )

    content = path.read_text(encoding="utf-8")
    assert legacy not in content
    assert 'model = "gpt"' in content


def test_register_removes_legacy_notify_from_a_toml_section(tmp_path: Path) -> None:
    codex_hooks = _module()
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "[notice.model_migrations]\n"
        "seen = true\n"
        'notify = ["/config/.codex/herdr-agent-state.sh"]\n',
        encoding="utf-8",
    )

    assert codex_hooks.register(
        tmp_path,
        "/config/.codex/herdr-agent-state.sh",
        label="herdr",
    )

    assert "notify" not in path.read_text(encoding="utf-8")


def test_register_preserves_user_notify(tmp_path: Path) -> None:
    codex_hooks = _module()
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('notify = ["my-notifier"]\n', encoding="utf-8")

    assert codex_hooks.register(tmp_path, "hook.sh", label="dash")

    assert path.read_text(encoding="utf-8") == 'notify = ["my-notifier"]\n'


def test_register_leaves_malformed_hooks_unchanged(tmp_path: Path, capsys) -> None:
    codex_hooks = _module()
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")

    assert not codex_hooks.register(tmp_path, "hook.sh", label="dash")

    assert path.read_text(encoding="utf-8") == "{broken"
    assert "hooks.json" in capsys.readouterr().out


def test_register_leaves_an_event_with_a_malformed_shape_unchanged(
    tmp_path: Path,
    capsys,
) -> None:
    codex_hooks = _module()
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"hooks": {"Stop": {"bad": "shape"}}}), encoding="utf-8")
    original = path.read_text(encoding="utf-8")

    assert not codex_hooks.register(tmp_path, "hook.sh", label="dash")

    assert path.read_text(encoding="utf-8") == original
    assert "Stop" in capsys.readouterr().out


@pytest.mark.parametrize("nested_hooks", [None, 7, {"command": "hook.sh"}])
def test_register_soft_fails_on_malformed_nested_hooks(
    tmp_path: Path,
    capsys,
    nested_hooks: object,
) -> None:
    codex_hooks = _module()
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": nested_hooks}]}}),
        encoding="utf-8",
    )
    original = path.read_text(encoding="utf-8")

    assert not codex_hooks.register(tmp_path, "hook.sh", label="dash")

    assert path.read_text(encoding="utf-8") == original
    assert "Stop" in capsys.readouterr().out


def test_register_leaves_malformed_toml_unchanged(tmp_path: Path, capsys) -> None:
    codex_hooks = _module()
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text("model = [unclosed\n", encoding="utf-8")

    assert codex_hooks.register(tmp_path, "hook.sh", label="dash")

    assert path.read_text(encoding="utf-8") == "model = [unclosed\n"
    assert "TOML" in capsys.readouterr().out


def test_registered_detects_exact_command_and_soft_fails(tmp_path: Path) -> None:
    codex_hooks = _module()
    assert not codex_hooks.registered(tmp_path, "hook.sh")
    assert codex_hooks.register(tmp_path, "hook.sh", label="dash")
    assert codex_hooks.registered(tmp_path, "hook.sh")
    assert not codex_hooks.registered(tmp_path, "other.sh")

    path = tmp_path / ".codex" / "hooks.json"
    path.write_text("{broken", encoding="utf-8")
    assert not codex_hooks.registered(tmp_path, "hook.sh")


def test_registered_rejects_a_partial_lifecycle_registration(tmp_path: Path) -> None:
    codex_hooks = _module()
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "hook.sh"}]},
                    ],
                },
            },
        ),
        encoding="utf-8",
    )

    assert not codex_hooks.registered(tmp_path, "hook.sh")


def test_registration_status_distinguishes_diagnostic_states(tmp_path: Path) -> None:
    codex_hooks = _module()
    path = tmp_path / ".codex" / "hooks.json"

    assert codex_hooks.registration_status(tmp_path, "hook.sh") == "missing"

    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    assert codex_hooks.registration_status(tmp_path, "hook.sh") == "malformed"

    path.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    assert codex_hooks.registration_status(tmp_path, "hook.sh") == "handler-absent"

    assert codex_hooks.register(tmp_path, "hook.sh", label="dash")
    assert codex_hooks.registration_status(tmp_path, "hook.sh") == "registered"


@pytest.mark.parametrize("nested_hooks", [None, 7, {"command": "hook.sh"}])
def test_registration_status_soft_fails_on_malformed_nested_hooks(
    tmp_path: Path,
    nested_hooks: object,
) -> None:
    codex_hooks = _module()
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": nested_hooks}]}}),
        encoding="utf-8",
    )

    assert codex_hooks.registration_status(tmp_path, "hook.sh") == "malformed"
    assert not codex_hooks.registered(tmp_path, "hook.sh")
