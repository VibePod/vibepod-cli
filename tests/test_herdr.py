"""Tests for herdr terminal-multiplexer integration (core/herdr.py)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import socket as socket_module
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from vibepod.constants import SUPPORTED_AGENTS
from vibepod.core import herdr

# sockaddr_un caps socket paths at ~104 bytes on macOS, which pytest's
# tmp_path exceeds under /private/var/folders. _make_socket binds at a short
# path and renames (callers only stat the result); tests that run a live
# server use the sock_dir fixture so connect() sees a short path too.
#
# Python on Windows exposes no socket.AF_UNIX (herdr integration is
# unsupported there — named pipes); the helpers below skip so every
# socket-dependent test reports as skipped instead of erroring.


def _skip_without_af_unix() -> None:
    if not hasattr(socket_module, "AF_UNIX"):
        pytest.skip("AF_UNIX sockets unavailable on this platform")


def _make_socket(path: Path) -> Path:
    _skip_without_af_unix()
    path.parent.mkdir(parents=True, exist_ok=True)
    sock = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    with tempfile.TemporaryDirectory(prefix="vp-sock-") as short_dir:
        short = Path(short_dir) / "s"
        sock.bind(str(short))
        sock.close()
        short.rename(path)
    return path


@pytest.fixture
def sock_dir() -> Iterator[Path]:
    _skip_without_af_unix()
    parent = Path(tempfile.mkdtemp(prefix="vp-sock-"))
    try:
        yield parent
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def test_inactive_without_herdr_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("HERDR_ENV", raising=False)
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(_make_socket(tmp_path / "herdr.sock")))
    assert herdr.herdr_active() is False


def test_inactive_when_socket_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(tmp_path / "missing.sock"))
    assert herdr.herdr_active() is False


def test_active_with_env_and_socket(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(_make_socket(tmp_path / "herdr.sock")))
    assert herdr.herdr_active() is True


def test_resolve_socket_falls_back_to_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    default = _make_socket(tmp_path / ".config" / "herdr" / "herdr.sock")
    assert herdr.resolve_socket() == default


def test_resolve_socket_returns_none_when_absent(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert herdr.resolve_socket() is None


def test_resolve_binary_prefers_env(monkeypatch, tmp_path: Path) -> None:
    binary = tmp_path / "herdr"
    binary.write_bytes(b"#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("HERDR_BIN_PATH", str(binary))
    assert herdr.resolve_binary() == binary


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="extension-less executable is not found by shutil.which on Windows",
)
def test_resolve_binary_falls_back_to_path(monkeypatch, tmp_path: Path) -> None:
    binary = tmp_path / "herdr"
    binary.write_bytes(b"#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.delenv("HERDR_BIN_PATH", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert herdr.resolve_binary() == binary


def test_resolve_binary_none_when_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("HERDR_BIN_PATH", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert herdr.resolve_binary() is None


def test_volumes_and_env_with_binary(monkeypatch, tmp_path: Path) -> None:
    sock = _make_socket(tmp_path / "herdr.sock")
    binary = tmp_path / "herdr"
    binary.write_bytes(b"#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(sock))
    monkeypatch.setenv("HERDR_BIN_PATH", str(binary))
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "pane-1")
    monkeypatch.setenv("HERDR_TAB_ID", "tab-1")
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "ws-1")

    volumes, env = herdr.herdr_volumes_and_env()

    assert (str(sock), "/herdr/herdr.sock", "rw") in volumes
    assert (str(binary), "/usr/local/bin/herdr", "ro") in volumes
    assert env["HERDR_SOCKET_PATH"] == "/herdr/herdr.sock"
    assert env["HERDR_BIN_PATH"] == "/usr/local/bin/herdr"
    assert env["HERDR_ENV"] == "1"
    assert env["HERDR_PANE_ID"] == "pane-1"
    assert env["HERDR_TAB_ID"] == "tab-1"
    assert env["HERDR_WORKSPACE_ID"] == "ws-1"


def test_volumes_and_env_without_binary(monkeypatch, tmp_path: Path) -> None:
    sock = _make_socket(tmp_path / "herdr.sock")
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(sock))
    monkeypatch.delenv("HERDR_BIN_PATH", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))  # no herdr on PATH
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "pane-1")
    monkeypatch.delenv("HERDR_TAB_ID", raising=False)
    monkeypatch.delenv("HERDR_WORKSPACE_ID", raising=False)

    volumes, env = herdr.herdr_volumes_and_env()

    assert volumes == [(str(sock), "/herdr/herdr.sock", "rw")]
    assert "HERDR_BIN_PATH" not in env
    assert env["HERDR_PANE_ID"] == "pane-1"
    assert "HERDR_TAB_ID" not in env


def test_volumes_and_env_without_socket(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(tmp_path / "missing.sock"))
    assert herdr.herdr_volumes_and_env() == ([], {})


def test_builtin_table_covers_expected_agents() -> None:
    assert set(herdr.BUILTIN_INTEGRATIONS) == {
        "claude",
        "codex",
        "copilot",
        "opencode",
        "pi",
        "tau",
    }


def test_builtin_resources_exist() -> None:
    for entries in herdr.BUILTIN_INTEGRATIONS.values():
        for resource_rel, _dest in entries:
            assert (herdr.resource_root() / resource_rel).is_file(), resource_rel


def test_sync_copies_builtin_files_with_exec_bit(tmp_path: Path) -> None:
    synced = herdr.sync_herdr_files("claude", tmp_path, {})
    target = tmp_path / "hooks" / "herdr-agent-state.sh"
    assert synced >= 1
    assert target.is_file()
    assert os.access(target, os.X_OK)


def test_sync_is_idempotent_and_overwrites_own_files(tmp_path: Path) -> None:
    herdr.sync_herdr_files("claude", tmp_path, {})
    target = tmp_path / "hooks" / "herdr-agent-state.sh"
    target.write_text("stale\n")
    herdr.sync_herdr_files("claude", tmp_path, {})
    assert target.read_text() != "stale\n"


def test_sync_leaves_unknown_agent_alone(tmp_path: Path) -> None:
    assert herdr.sync_herdr_files("gemini", tmp_path, {}) == 0
    assert list(tmp_path.iterdir()) == []


def test_sync_custom_config_entry(tmp_path: Path) -> None:
    source = tmp_path / "my-hook.sh"
    source.write_text("#!/bin/sh\nexit 0\n")
    source.chmod(0o755)
    config = {
        "herdr": {
            "integrations": {"gemini": [{"source": str(source), "dest": "hooks/my-hook.sh"}]},
        },
    }
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    assert herdr.sync_herdr_files("gemini", config_dir, config) == 1
    assert (config_dir / "hooks" / "my-hook.sh").is_file()


def test_sync_warns_on_missing_custom_source(tmp_path: Path, capsys) -> None:
    config = {
        "herdr": {
            "integrations": {
                "gemini": [{"source": str(tmp_path / "gone.sh"), "dest": "hooks/x.sh"}],
            },
        },
    }
    assert herdr.sync_herdr_files("gemini", tmp_path, config) == 0
    assert "gone.sh" in capsys.readouterr().out


def test_sync_rejects_dest_escaping_config_dir(tmp_path: Path) -> None:
    source = tmp_path / "h.sh"
    source.write_text("#!/bin/sh\n")
    config = {
        "herdr": {"integrations": {"gemini": [{"source": str(source), "dest": "../evil.sh"}]}},
    }
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    assert herdr.sync_herdr_files("gemini", config_dir, config) == 0
    assert not (tmp_path / "evil.sh").exists()


def test_claude_settings_created_with_hooks(tmp_path: Path) -> None:
    herdr.register_claude_hooks(tmp_path)
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert set(settings["hooks"]) >= {
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "Notification",
        "Stop",
        "SessionEnd",
    }
    command = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert "herdr-agent-state.sh" in command


def test_claude_settings_merge_preserves_user_entries(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "model": "opus",
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "my-own.sh"}]}]},
            },
        ),
    )
    herdr.register_claude_hooks(tmp_path)
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert settings["model"] == "opus"
    stop_commands = [
        hook["command"] for entry in settings["hooks"]["Stop"] for hook in entry["hooks"]
    ]
    assert "my-own.sh" in stop_commands
    assert any("herdr-agent-state.sh" in cmd for cmd in stop_commands)


def test_claude_settings_merge_is_idempotent(tmp_path: Path) -> None:
    herdr.register_claude_hooks(tmp_path)
    first = (tmp_path / "settings.json").read_text()
    herdr.register_claude_hooks(tmp_path)
    assert (tmp_path / "settings.json").read_text() == first


def test_claude_settings_unparseable_left_alone(tmp_path: Path, capsys) -> None:
    (tmp_path / "settings.json").write_text("{broken")
    herdr.register_claude_hooks(tmp_path)
    assert (tmp_path / "settings.json").read_text() == "{broken"
    assert "settings.json" in capsys.readouterr().out


def test_codex_notify_registered(tmp_path: Path) -> None:
    herdr.register_codex_notify(tmp_path)
    content = (tmp_path / ".codex" / "config.toml").read_text()
    assert 'notify = ["/config/.codex/herdr-agent-state.sh"]' in content


def test_codex_notify_appends_to_existing_config(tmp_path: Path) -> None:
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text('model = "o4"\n')
    herdr.register_codex_notify(tmp_path)
    content = (codex_dir / "config.toml").read_text()
    assert 'model = "o4"' in content
    assert "herdr-agent-state.sh" in content


def test_codex_notify_respects_existing_notify(tmp_path: Path, capsys) -> None:
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text('notify = ["my-notifier"]\n')
    herdr.register_codex_notify(tmp_path)
    assert "my-notifier" in (codex_dir / "config.toml").read_text()
    assert "herdr-agent-state.sh" not in (codex_dir / "config.toml").read_text()
    assert "notify" in capsys.readouterr().out


def test_codex_notify_idempotent(tmp_path: Path) -> None:
    herdr.register_codex_notify(tmp_path)
    first = (tmp_path / ".codex" / "config.toml").read_text()
    herdr.register_codex_notify(tmp_path)
    assert (tmp_path / ".codex" / "config.toml").read_text() == first


def _activate(monkeypatch, tmp_path: Path, with_binary: bool = True) -> None:
    sock = _make_socket(tmp_path / "herdr.sock")
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(sock))
    monkeypatch.setenv("HERDR_PANE_ID", "pane-1")
    if with_binary:
        binary = tmp_path / "herdr"
        binary.write_bytes(b"#!/bin/sh\n")
        binary.chmod(0o755)
        monkeypatch.setenv("HERDR_BIN_PATH", str(binary))
    else:
        monkeypatch.delenv("HERDR_BIN_PATH", raising=False)
        monkeypatch.setenv("PATH", str(tmp_path))


def test_apply_noop_when_flag_disables(monkeypatch, tmp_path: Path) -> None:
    _activate(monkeypatch, tmp_path)
    volumes, env = herdr.apply_herdr_if_enabled("claude", tmp_path / "cfg", {}, no_herdr=True)
    assert (volumes, env) == ([], {})


def test_apply_noop_when_config_disables(monkeypatch, tmp_path: Path) -> None:
    _activate(monkeypatch, tmp_path)
    volumes, env = herdr.apply_herdr_if_enabled(
        "claude",
        tmp_path / "cfg",
        {"herdr": False},
        no_herdr=False,
    )
    assert (volumes, env) == ([], {})


def test_apply_noop_outside_herdr(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("HERDR_ENV", raising=False)
    volumes, env = herdr.apply_herdr_if_enabled("claude", tmp_path / "cfg", {}, no_herdr=False)
    assert (volumes, env) == ([], {})


def test_apply_wires_claude(monkeypatch, tmp_path: Path) -> None:
    _activate(monkeypatch, tmp_path)
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    volumes, env = herdr.apply_herdr_if_enabled("claude", config_dir, {}, no_herdr=False)
    assert any(dest == "/herdr/herdr.sock" for _, dest, _ in volumes)
    assert env["HERDR_SOCKET_PATH"] == "/herdr/herdr.sock"
    assert (config_dir / "hooks" / "herdr-agent-state.sh").is_file()
    assert (config_dir / "settings.json").is_file()


def test_apply_wires_codex_notify(monkeypatch, tmp_path: Path) -> None:
    _activate(monkeypatch, tmp_path)
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    herdr.apply_herdr_if_enabled("codex", config_dir, {}, no_herdr=False)
    assert (config_dir / ".codex" / "config.toml").is_file()


def test_apply_warns_without_binary(monkeypatch, tmp_path: Path, capsys) -> None:
    _activate(monkeypatch, tmp_path, with_binary=False)
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    volumes, env = herdr.apply_herdr_if_enabled("claude", config_dir, {}, no_herdr=False)
    assert "HERDR_BIN_PATH" not in env
    assert len(volumes) == 1
    assert "herdr binary" in capsys.readouterr().out


def test_run_signature_has_no_herdr_flag() -> None:
    import inspect

    from vibepod.commands import run as run_module

    assert "no_herdr" in inspect.signature(run_module.run).parameters


def test_cli_run_command_threads_no_herdr() -> None:
    import inspect

    from vibepod import cli

    assert "no_herdr" in inspect.signature(cli.run_command).parameters


def test_cli_alias_threads_no_herdr() -> None:
    # Introspect the click command instead of parsing --help output: rich
    # renders the help at terminal-dependent widths, which breaks substring
    # matching on CI.
    from typer.main import get_command

    from vibepod.cli import app

    run_cmd = get_command(app).commands["run"]  # type: ignore[attr-defined]
    opts = [opt for param in run_cmd.params for opt in param.opts]
    assert "--no-herdr" in opts


def test_task_commands_thread_no_herdr() -> None:
    import inspect

    from vibepod.commands import task as task_module

    assert "no_herdr" in inspect.signature(task_module.task_create_command).parameters
    assert "no_herdr" in inspect.signature(task_module.task_run_command).parameters


def test_claude_settings_non_list_event_skipped(tmp_path: Path, capsys) -> None:
    (tmp_path / "settings.json").write_text(json.dumps({"hooks": {"Stop": {"bad": "shape"}}}))
    herdr.register_claude_hooks(tmp_path)
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert settings["hooks"]["Stop"] == {"bad": "shape"}
    assert any("herdr-agent-state.sh" in c for c in json.dumps(settings["hooks"]).split('"'))
    assert "Stop" in capsys.readouterr().out


def test_apply_logs_detection_without_integrations(monkeypatch, tmp_path: Path, capsys) -> None:
    _activate(monkeypatch, tmp_path)
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    herdr.apply_herdr_if_enabled("gemini", config_dir, {}, no_herdr=False)
    assert "herdr pane detected" in capsys.readouterr().out


def test_codex_notify_goes_to_root_table_before_sections(tmp_path: Path) -> None:
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text(
        'model = "o4"\n\n[notice.model_migrations]\nseen = true\n',
    )
    herdr.register_codex_notify(tmp_path)
    parsed = herdr.tomllib.loads((codex_dir / "config.toml").read_text())
    assert parsed["notify"] == ["/config/.codex/herdr-agent-state.sh"]
    assert "notify" not in parsed["notice"]["model_migrations"]


def test_codex_notify_repairs_line_misplaced_inside_section(tmp_path: Path) -> None:
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text(
        "[notice.model_migrations]\nseen = true\n"
        'notify = ["/config/.codex/herdr-agent-state.sh"]\n',
    )
    herdr.register_codex_notify(tmp_path)
    parsed = herdr.tomllib.loads((codex_dir / "config.toml").read_text())
    assert parsed["notify"] == ["/config/.codex/herdr-agent-state.sh"]
    assert "notify" not in parsed["notice"]["model_migrations"]


def test_codex_notify_skips_invalid_toml(tmp_path: Path, capsys) -> None:
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text("model = [unclosed\n")
    herdr.register_codex_notify(tmp_path)
    assert (codex_dir / "config.toml").read_text() == "model = [unclosed\n"
    assert "TOML" in capsys.readouterr().out


def test_codex_notify_respects_user_notify_in_root(tmp_path: Path, capsys) -> None:
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text('notify = ["my-notifier"]\n\n[other]\nx = 1\n')
    herdr.register_codex_notify(tmp_path)
    content = (codex_dir / "config.toml").read_text()
    assert "my-notifier" in content
    assert "herdr-agent-state.sh" not in content


def test_doctor_herdr_command_registered() -> None:
    from typer.testing import CliRunner

    from vibepod.cli import app

    result = CliRunner().invoke(app, ["doctor", "herdr", "--help"])
    assert result.exit_code == 0
    assert "herdr" in result.output


def test_doctor_herdr_reports_missing_pane(monkeypatch) -> None:
    from typer.testing import CliRunner

    from vibepod.cli import app

    for key in ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/nonexistent/herdr.sock")
    result = CliRunner().invoke(app, ["doctor", "herdr", "claude"])
    assert result.exit_code == 1
    assert "MISSING" in result.output


def test_elf_linkage_classifier(tmp_path: Path) -> None:
    import sys

    from vibepod.commands.doctor import _elf_linkage

    script = tmp_path / "wrapper"
    script.write_bytes(b"#!/bin/sh\nexec real\n")
    assert _elf_linkage(script) == "script: /bin/sh"
    if sys.platform.startswith("linux"):  # /bin/ls is Mach-O on macOS
        assert _elf_linkage(Path("/bin/ls")).startswith(("dynamic, needs ", "static"))
    junk = tmp_path / "junk"
    junk.write_bytes(b"MZ\x00\x00")
    assert _elf_linkage(junk) == "not an ELF executable"


def test_hook_script_reports_via_socket_end_to_end(tmp_path: Path, sock_dir: Path) -> None:
    """Full chain: claude hook script -> node herdr-report.js -> unix socket."""
    import shutil as shutil_module
    import subprocess
    import threading

    if shutil_module.which("node") is None:
        import pytest

        pytest.skip("node not available")

    received: list[dict] = []
    sock_path = sock_dir / "herdr.sock"
    server = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)

    def serve() -> None:
        conn, _ = server.accept()
        data = conn.recv(65536).decode()
        received.append(json.loads(data.splitlines()[0]))
        conn.sendall(b'{"id":"x","result":{"type":"pane_info"}}\n')
        conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    herdr.sync_herdr_files("claude", config_dir, {})
    script = config_dir / "hooks" / "herdr-agent-state.sh"

    proc = subprocess.run(
        ["sh", str(script)],
        input='{"hook_event_name":"UserPromptSubmit","session_id":"s1"}',
        capture_output=True,
        text=True,
        timeout=15,
        env={
            "PATH": os.environ["PATH"],
            "HERDR_SOCKET_PATH": str(sock_path),
            "HERDR_PANE_ID": "w1:p1",
            "CLAUDE_CONFIG_DIR": str(config_dir),
        },
    )
    thread.join(timeout=10)
    server.close()

    assert proc.returncode == 0
    assert received, "no request reached the socket"
    request = received[0]
    assert request["method"] == "pane.report_agent"
    assert request["params"] == {
        "pane_id": "w1:p1",
        "source": "vibepod",
        "agent": "claude",
        "display_agent": "vp:claude",
        "state": "working",
        "agent_session_id": "s1",
    }
    log_text = (config_dir / "herdr-hook.log").read_text()
    assert "via=socket rc=0" in log_text


def _serve_requests(sock_path: Path, received: list, count: int) -> object:
    import threading

    server = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(count)

    def serve() -> None:
        for _ in range(count):
            conn, _ = server.accept()
            received.append(json.loads(conn.recv(65536).decode().splitlines()[0]))
            conn.sendall(b'{"id":"x","result":{}}\n')
            conn.close()
        server.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread


def _serve_one(sock_path: Path, received: list) -> object:
    return _serve_requests(sock_path, received, 1)


def test_release_agent_sends_socket_request(monkeypatch, sock_dir: Path) -> None:
    received: list = []
    thread = _serve_one(sock_dir / "herdr.sock", received)
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(sock_dir / "herdr.sock"))
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")

    assert herdr.release_agent("claude") is True
    thread.join(timeout=5)
    assert received[0]["method"] == "pane.release_agent"
    assert received[0]["params"] == {
        "pane_id": "w1:p1",
        "source": "vibepod",
        "agent": "claude",
    }


def test_release_agent_noop_without_socket(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(tmp_path / "missing.sock"))
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    assert herdr.release_agent("claude") is False


def test_release_agent_noop_without_pane(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(_make_socket(tmp_path / "herdr.sock")))
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    assert herdr.release_agent("claude") is False


def test_run_module_imports_release() -> None:
    from vibepod.commands import run as run_module

    assert callable(run_module._release_herdr_agent)


def test_release_agent_accepts_explicit_pane(monkeypatch, sock_dir: Path) -> None:
    received: list = []
    thread = _serve_one(sock_dir / "herdr.sock", received)
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(sock_dir / "herdr.sock"))
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)

    assert herdr.release_agent("codex", pane="w9:p9") is True
    thread.join(timeout=5)
    assert received[0]["params"]["pane_id"] == "w9:p9"
    assert received[0]["params"]["agent"] == "codex"


def test_herdr_pane_label_constant() -> None:
    assert herdr.PANE_LABEL == "vibepod.herdr.pane"


def test_stop_releases_from_labels(monkeypatch, sock_dir: Path) -> None:
    from vibepod.commands import stop as stop_module

    received: list = []
    thread = _serve_one(sock_dir / "herdr.sock", received)
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(sock_dir / "herdr.sock"))
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)

    class FakeContainer:
        labels = {"vibepod.agent": "codex", herdr.PANE_LABEL: "w2:p3"}

    stop_module._release_herdr_entries([FakeContainer()])
    thread.join(timeout=5)
    assert received[0]["params"] == {
        "pane_id": "w2:p3",
        "source": "vibepod",
        "agent": "codex",
    }


def test_agent_label_prefixes_vp() -> None:
    assert herdr.agent_label("claude") == "vp:claude"


def test_release_agent_custom_source(monkeypatch, sock_dir: Path) -> None:
    received: list = []
    thread = _serve_one(sock_dir / "herdr.sock", received)
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(sock_dir / "herdr.sock"))
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")

    assert herdr.release_agent("claude", source="vibepod:doctor") is True
    thread.join(timeout=5)
    assert received[0]["params"]["source"] == "vibepod:doctor"


def test_doctor_herdr_summary_lists_all_agents(monkeypatch, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from vibepod.cli import app
    from vibepod.constants import SUPPORTED_AGENTS

    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    monkeypatch.setenv("HERDR_TAB_ID", "w1:t1")
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w1")
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(_make_socket(tmp_path / "herdr.sock")))
    monkeypatch.setenv("HERDR_BIN_PATH", "/nonexistent/herdr")

    result = CliRunner().invoke(app, ["doctor", "herdr"])
    for agent_name in SUPPORTED_AGENTS:
        assert agent_name in result.output


def test_release_agent_reports_error_reply(monkeypatch, sock_dir: Path, capsys) -> None:
    import threading

    sock_path = sock_dir / "herdr.sock"
    server = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)

    def serve() -> None:
        conn, _ = server.accept()
        conn.recv(65536)
        conn.sendall(b'{"id":"x","error":{"message":"unknown agent"}}\n')
        conn.close()
        server.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(sock_path))
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")

    assert herdr.release_agent("codex") is False
    thread.join(timeout=5)
    assert "unknown agent" in capsys.readouterr().out


def test_release_agent_label_override(monkeypatch, sock_dir: Path) -> None:
    received: list = []
    thread = _serve_one(sock_dir / "herdr.sock", received)
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(sock_dir / "herdr.sock"))
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")

    assert herdr.release_agent("codex", label="codex") is True
    thread.join(timeout=5)
    assert received[0]["params"]["agent"] == "codex"


def _fake_herdr_binary(tmp_path: Path, rc: int = 0, reject_flag: str = "") -> Path:
    """Fake herdr CLI logging argv; optionally errors when a flag is present."""
    if sys.platform == "win32":
        pytest.skip("fake herdr binary is a shell script; Windows cannot execute it")
    binary = tmp_path / "herdr"
    log = tmp_path / "herdr-argv.log"
    script = "#!/bin/sh\n" + (
        f'case "$*" in *"{reject_flag}"*) echo "unknown option: {reject_flag}" >&2; exit 2;; esac\n'
        if reject_flag
        else ""
    )
    script += f'echo "$@" >> "{log}"\nexit {rc}\n'
    binary.write_text(script)
    binary.chmod(0o755)
    return binary


def test_report_pane_metadata_sets_title_and_display(monkeypatch, tmp_path: Path) -> None:
    binary = _fake_herdr_binary(tmp_path)
    monkeypatch.setenv("HERDR_BIN_PATH", str(binary))
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")

    assert herdr.report_pane_metadata("codex") is True
    argv = (tmp_path / "herdr-argv.log").read_text()
    assert "pane report-metadata w1:p1" in argv
    assert "--title vp:codex" in argv
    assert "--display-agent vp:codex" in argv


def test_report_pane_metadata_falls_back_without_display_flag(monkeypatch, tmp_path: Path) -> None:
    binary = _fake_herdr_binary(tmp_path, reject_flag="--display-agent")
    monkeypatch.setenv("HERDR_BIN_PATH", str(binary))
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")

    assert herdr.report_pane_metadata("codex") is True
    argv = (tmp_path / "herdr-argv.log").read_text()
    assert "--title vp:codex" in argv
    assert "--display-agent" not in argv


def test_clear_pane_metadata_clears_title(monkeypatch, tmp_path: Path) -> None:
    binary = _fake_herdr_binary(tmp_path, reject_flag="--clear-display-agent")
    monkeypatch.setenv("HERDR_BIN_PATH", str(binary))
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")

    herdr.clear_pane_metadata("codex")
    argv = (tmp_path / "herdr-argv.log").read_text()
    assert "--clear-title" in argv


def test_report_pane_metadata_noop_without_binary(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("HERDR_BIN_PATH", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    assert herdr.report_pane_metadata("codex") is False


@pytest.mark.parametrize("agent", SUPPORTED_AGENTS)
def test_report_pane_metadata_reports_vp_label_via_socket(
    monkeypatch,
    tmp_path: Path,
    sock_dir: Path,
    agent: str,
) -> None:
    received: list = []
    thread = _serve_one(sock_dir / "herdr.sock", received)
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(sock_dir / "herdr.sock"))
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    monkeypatch.delenv("HERDR_BIN_PATH", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert herdr.report_pane_metadata(agent) is True
    thread.join(timeout=5)
    assert received[0]["method"] == "pane.report_agent"
    assert received[0]["params"] == {
        "pane_id": "w1:p1",
        "source": "vibepod",
        "agent": agent,
        "display_agent": f"vp:{agent}",
        "state": "idle",
    }


def test_tau_extension_reports_lifecycle_states(monkeypatch, sock_dir: Path) -> None:
    extension_path = herdr.resource_root() / "tau" / "extensions" / "herdr_agent_state.py"
    spec = importlib.util.spec_from_file_location("vibepod_tau_herdr", extension_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    handlers: dict[str, object] = {}

    class FakeTau:
        def on(self, event: str, handler=None):
            if handler is not None:
                handlers[event] = handler
                return handler

            def register(decorated):
                handlers[event] = decorated
                return decorated

            return register

    module.setup(FakeTau())
    assert set(handlers) >= {"agent_start", "turn_start", "agent_end", "turn_end"}

    received: list = []
    thread = _serve_requests(sock_dir / "herdr.sock", received, 2)
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(sock_dir / "herdr.sock"))
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")

    class Context:
        session_id = "tau-session-1"

    async def exercise() -> None:
        await handlers["turn_start"](object(), Context())
        await handlers["turn_end"](object(), Context())

    asyncio.run(exercise())
    thread.join(timeout=5)

    assert [request["params"]["state"] for request in received] == ["working", "idle"]
    assert all(request["method"] == "pane.report_agent" for request in received)
    assert all(request["params"]["agent"] == "tau" for request in received)
    assert all(request["params"]["display_agent"] == "vp:tau" for request in received)
    assert all(request["params"]["agent_session_id"] == "tau-session-1" for request in received)


def test_reexec_sets_hint_and_execs(monkeypatch, tmp_path: Path) -> None:
    calls: list = []
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(_make_socket(tmp_path / "herdr.sock")))
    monkeypatch.delenv("HERDR_AGENT", raising=False)
    monkeypatch.setattr(herdr.os, "execvp", lambda file, args: calls.append((file, args)))
    monkeypatch.setattr(herdr.sys, "argv", ["vp", "run", "codex"])

    herdr.reexec_with_agent_hint("codex", {}, no_herdr=False)

    assert calls == [("vp", ["vp", "run", "codex"])]
    assert herdr.os.environ["HERDR_AGENT"] == "codex"


def test_reexec_skips_when_hint_already_set(monkeypatch, tmp_path: Path) -> None:
    calls: list = []
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(_make_socket(tmp_path / "herdr.sock")))
    monkeypatch.setenv("HERDR_AGENT", "codex")
    monkeypatch.setattr(herdr.os, "execvp", lambda file, args: calls.append((file, args)))

    herdr.reexec_with_agent_hint("codex", {}, no_herdr=False)
    assert calls == []


def test_reexec_skips_outside_pane_or_disabled(monkeypatch, tmp_path: Path) -> None:
    calls: list = []
    monkeypatch.setattr(herdr.os, "execvp", lambda file, args: calls.append((file, args)))
    monkeypatch.delenv("HERDR_ENV", raising=False)
    herdr.reexec_with_agent_hint("codex", {}, no_herdr=False)

    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(_make_socket(tmp_path / "herdr.sock")))
    monkeypatch.delenv("HERDR_AGENT", raising=False)
    herdr.reexec_with_agent_hint("codex", {}, no_herdr=True)
    herdr.reexec_with_agent_hint("codex", {"herdr": False}, no_herdr=False)
    assert calls == []
