"""Opt-in syd process sandbox: policy rendering, prerequisites and launch wiring."""

from __future__ import annotations

import os
import shutil
import stat
import struct
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vibepod.cli import app
from vibepod.commands import run as run_cmd
from vibepod.commands import task as task_cmd
from vibepod.core import sandbox
from vibepod.core.sandbox import (
    SANDBOX_LABEL,
    SYD_BINARY_MOUNT,
    SYD_POLICY_MOUNT,
    SandboxError,
    SydSandbox,
    render_syd_policy,
    resolve_sandbox_type,
    syd_settings,
    validate_syd_binary,
    wrap_command,
    writable_targets,
    write_policy,
)
from vibepod.core.tasks import TaskStore

_SYD_PREFIX = [SYD_BINARY_MOUNT, "-P", SYD_POLICY_MOUNT, "--"]


def _write_elf(path: Path, *, interp: bool = False, machine: int = 62) -> Path:
    """Write a minimal ELF64 header with one program header."""
    header = b"\x7fELF" + bytes([2, 1, 1]) + bytes(9)
    header += struct.pack("<HHIQQQIHHHHHH", 2, machine, 1, 0, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    program = struct.pack("<II", 3 if interp else 1, 0) + bytes(48)
    path.write_bytes(header + program)
    path.chmod(0o755)
    return path


@pytest.fixture
def syd_binary(tmp_path: Path) -> Path:
    return _write_elf(tmp_path / "syd")


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path / "vpcfg"))
    # The host kernel is not under test here; check_host_kernel has its own tests.
    monkeypatch.setattr(sandbox, "check_host_kernel", lambda: None)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_sandbox_defaults_to_none() -> None:
    assert resolve_sandbox_type({}) == "none"
    assert resolve_sandbox_type({"sandbox": {"type": "none"}}) == "none"


def test_sandbox_type_from_config_and_flag_override() -> None:
    config = {"sandbox": {"type": "syd"}}
    assert resolve_sandbox_type(config) == "syd"
    assert resolve_sandbox_type(config, "none") == "none"
    assert resolve_sandbox_type({}, "SYD") == "syd"


def test_unknown_sandbox_type_is_rejected() -> None:
    with pytest.raises(SandboxError, match="Supported: none, syd"):
        resolve_sandbox_type({}, "bwrap")


def test_vp_sandbox_env_overrides_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from vibepod.core.config import get_config

    monkeypatch.setenv("VP_SANDBOX", "syd")
    monkeypatch.setenv("VP_SANDBOX_SYD_BINARY", "/opt/syd/bin/syd")
    config = get_config()
    assert config["sandbox"]["type"] == "syd"
    assert config["sandbox"]["syd"]["binary"] == "/opt/syd/bin/syd"


def test_syd_settings_validates_log_level_and_rules() -> None:
    assert syd_settings({})["log_level"] == "warn"
    with pytest.raises(SandboxError, match="log_level"):
        syd_settings({"sandbox": {"syd": {"log_level": "loud"}}})
    with pytest.raises(SandboxError, match="list of syd rule strings"):
        syd_settings({"sandbox": {"syd": {"rules": "deny/exec+/usr/bin/curl"}}})
    with pytest.raises(SandboxError, match="single line"):
        syd_settings({"sandbox": {"syd": {"rules": ["deny/exec+/a\ninclude /x"]}}})


# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------


def test_missing_binary_setting_explains_setup() -> None:
    with pytest.raises(SandboxError, match="sandbox.syd.binary") as exc:
        validate_syd_binary("")
    assert "distfiles.exherbo.org" in str(exc.value)


def test_binary_not_found(tmp_path: Path) -> None:
    with pytest.raises(SandboxError, match="not found"):
        validate_syd_binary(str(tmp_path / "nope"))


def test_binary_not_executable(syd_binary: Path) -> None:
    syd_binary.chmod(0o644)
    with pytest.raises(SandboxError, match="chmod \\+x"):
        validate_syd_binary(str(syd_binary))


def test_binary_not_elf(tmp_path: Path) -> None:
    script = tmp_path / "syd"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    with pytest.raises(SandboxError, match="not a Linux ELF"):
        validate_syd_binary(str(script))


def test_dynamic_binary_is_rejected(tmp_path: Path) -> None:
    binary = _write_elf(tmp_path / "syd", interp=True)
    with pytest.raises(SandboxError, match="dynamically linked"):
        validate_syd_binary(str(binary))


def test_static_binary_is_accepted(tmp_path: Path) -> None:
    binary = _write_elf(tmp_path / "syd", machine=183)
    path, arch = validate_syd_binary(str(binary))
    assert path == binary.resolve()
    assert arch == "aarch64"


def _fake_host(monkeypatch: pytest.MonkeyPatch, platform: str, release: str) -> None:
    # Drop the autouse no-op so the real check runs against the fake host.
    monkeypatch.undo()
    monkeypatch.setattr(sandbox.sys, "platform", platform)
    monkeypatch.setattr(sandbox.os, "uname", lambda: os.uname_result(("", "", release, "", "")))


def test_old_linux_kernel_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_host(monkeypatch, "linux", "5.15.0-1-x")
    with pytest.raises(SandboxError, match="5.19 or newer"):
        sandbox.check_host_kernel()
    _fake_host(monkeypatch, "linux", "6.8.0")
    sandbox.check_host_kernel()


def test_kernel_check_is_skipped_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_host(monkeypatch, "darwin", "23.0.0")
    sandbox.check_host_kernel()


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def _policy(**kwargs) -> list[str]:  # type: ignore[no-untyped-def]
    kwargs.setdefault("agent", "claude")
    kwargs.setdefault("workspace_paths", ["/workspace"])
    kwargs.setdefault("writable_paths", ["/claude"])
    return render_syd_policy(**kwargs).splitlines()


def test_policy_confines_writes_and_protects_git_metadata() -> None:
    lines = _policy()
    writes = "wpath,cpath,dpath,tpath,fattr,symlink"
    deny_all = lines.index(f"deny/{writes}+/***")
    assert lines.index("allow/all+/***") < deny_all
    workspace = lines.index(f"allow/{writes}+/workspace/***")
    assert deny_all < lines.index(f"allow/{writes}+/claude/***") < workspace
    # Last match wins in syd: the git denies must follow the workspace allow.
    for entry in ("/workspace/.git", "/workspace/.git/config", "/workspace/.git/hooks/***"):
        assert lines.index(f"deny/{writes}+{entry}") > workspace


def test_policy_keeps_environment_and_jit_runtimes_working() -> None:
    lines = _policy()
    for required in ("passenv+*", "include_profile nomem", "include_profile nopie"):
        assert required in lines
    assert "sandbox/lock:off" in lines
    assert "log/level:warn" in lines


def test_policy_protects_every_workspace_mount() -> None:
    lines = _policy(workspace_paths=["/workspace", "/home/me/proj", None])
    assert "deny/wpath,cpath,dpath,tpath,fattr,symlink+/home/me/proj/.git/hooks/***" in lines


def test_policy_escapes_glob_characters_in_paths() -> None:
    lines = _policy(workspace_paths=["/workspace", "/src/a*b[1]"])
    assert "allow/wpath,cpath,dpath,tpath,fattr,symlink+/src/a\\*b\\[1\\]/***" in lines


def test_policy_appends_user_rules_last() -> None:
    lines = _policy(rules=["deny/exec+/usr/bin/curl"], log_level="error")
    assert lines[-1] == "deny/exec+/usr/bin/curl"
    assert "log/level:error" in lines


def test_policy_skips_relative_and_root_paths() -> None:
    lines = _policy(writable_paths=["relative", "/", "/claude", "/claude/"])
    assert not any(line.endswith("+//***") or "relative" in line for line in lines)
    assert lines.count("allow/wpath,cpath,dpath,tpath,fattr,symlink+/claude/***") == 1


def test_write_policy_is_content_addressed_and_not_group_writable(tmp_path: Path) -> None:
    first = write_policy("allow/all+/***\n", tmp_path)
    again = write_policy("allow/all+/***\n", tmp_path)
    other = write_policy("deny/all+/***\n", tmp_path)
    assert first == again != other
    assert stat.S_IMODE(first.stat().st_mode) == 0o644
    assert first.read_text() == "allow/all+/***\n"


def test_writable_targets_skip_read_only_and_sandbox_mounts() -> None:
    targets = writable_targets(
        config_mount_path="/config",
        env={"HOME": "/home/agent"},
        volumes=[
            ("/h/a", "/root/.augment", "rw"),
            ("/h/ca", "/etc/vibepod-proxy-ca", "ro"),
            ("/h/syd", SYD_BINARY_MOUNT, "rw"),
        ],
    )
    assert targets == ["/config", "/home/agent", "/root/.augment"]


def test_wrap_command_inserts_after_native_prefix(tmp_path: Path) -> None:
    syd = SydSandbox(binary=tmp_path / "syd", policy_path=tmp_path / "p.syd-3")
    assert wrap_command(["/entry.sh", "claude", "-p"], 1, syd) == [
        "/entry.sh",
        *_SYD_PREFIX,
        "claude",
        "-p",
    ]
    assert syd.volumes() == [
        (str(tmp_path / "syd"), SYD_BINARY_MOUNT, "ro"),
        (str(tmp_path / "p.syd-3"), SYD_POLICY_MOUNT, "ro"),
    ]


# ---------------------------------------------------------------------------
# vp run / vp task wiring
# ---------------------------------------------------------------------------


class _Manager:
    def __init__(self, image_cmd: list[str] | None = None) -> None:
        self.run_kwargs: dict | None = None
        self._image_cmd = image_cmd or []

    def ensure_network(self, name: str) -> None:
        pass

    def networks_with_running_containers(self) -> list[str]:
        return []

    def pull_image(self, image: str, auto_clean: bool = False) -> None:
        pass

    def image_cmd(self, image: str) -> list[str]:
        return list(self._image_cmd)

    def resolve_launch_command(self, image: str, command: list[str] | None) -> list[str]:
        return ["/entrypoint.sh", *(command or self._image_cmd)]

    def run_agent(self, **kwargs):  # type: ignore[no-untyped-def]
        self.run_kwargs = kwargs
        return type(
            "_Container",
            (),
            {
                "id": "cid123456789012",
                "name": "vibepod-test",
                "status": "running",
                "attrs": {"NetworkSettings": {"Networks": {}}},
                "reload": lambda self: None,
                "labels": {},
                "logs": lambda self, **kw: b"",
            },
        )()


def _config(binary: Path | None, sandbox_type: str = "none", **agent_cfg) -> dict:  # type: ignore[no-untyped-def]
    agent = {"env": {}, "init": [], **agent_cfg}
    return {
        "default_agent": "claude",
        "auto_pull": False,
        "auto_remove": True,
        "network": "vibepod-network",
        "agents": {"claude": dict(agent), "vibe": dict(agent)},
        "proxy": {"enabled": False},
        "logging": {"enabled": False},
        "sandbox": {
            "type": sandbox_type,
            "syd": {"binary": str(binary) if binary else "", "rules": []},
        },
    }


@pytest.fixture
def run_env(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(run_cmd, "is_dir_allowed", lambda p: True)

    def _setup(config: dict, manager: _Manager | None = None) -> _Manager:
        stub = manager or _Manager()
        monkeypatch.setattr(run_cmd, "get_config", lambda: config)
        monkeypatch.setattr(run_cmd, "DockerManager", lambda: stub)
        return stub

    return _setup


def _volume_targets(kwargs: dict) -> dict[str, tuple[str, str]]:
    return {target: (host, mode) for host, target, mode in kwargs["extra_volumes"]}


def test_run_default_leaves_launch_unchanged(run_env, tmp_path: Path, syd_binary: Path) -> None:
    stub = run_env(_config(syd_binary))
    run_cmd.run(agent="claude", workspace=tmp_path, detach=True)
    assert stub.run_kwargs is not None
    assert stub.run_kwargs["command"] == ["claude"]
    assert SYD_BINARY_MOUNT not in _volume_targets(stub.run_kwargs)
    assert SANDBOX_LABEL not in stub.run_kwargs["extra_labels"]


def test_run_sandbox_flag_wraps_agent_command(run_env, tmp_path: Path, syd_binary: Path) -> None:
    stub = run_env(_config(syd_binary))
    run_cmd.run(agent="claude", workspace=tmp_path, detach=True, ikwid=True, sandbox="syd")
    assert stub.run_kwargs is not None
    assert stub.run_kwargs["command"] == [*_SYD_PREFIX, "claude", "--dangerously-skip-permissions"]
    volumes = _volume_targets(stub.run_kwargs)
    assert volumes[SYD_BINARY_MOUNT] == (str(syd_binary.resolve()), "ro")
    policy_host, mode = volumes[SYD_POLICY_MOUNT]
    assert mode == "ro"
    policy = Path(policy_host).read_text()
    assert "allow/wpath,cpath,dpath,tpath,fattr,symlink+/claude/***" in policy
    assert "deny/wpath,cpath,dpath,tpath,fattr,symlink+/workspace/.git/hooks/***" in policy
    assert stub.run_kwargs["extra_labels"][SANDBOX_LABEL] == "syd"


def test_run_project_config_enables_and_flag_disables(
    run_env, tmp_path: Path, syd_binary: Path
) -> None:
    stub = run_env(_config(syd_binary, sandbox_type="syd"))
    run_cmd.run(agent="claude", workspace=tmp_path, detach=True)
    assert stub.run_kwargs is not None
    assert stub.run_kwargs["command"][: len(_SYD_PREFIX)] == _SYD_PREFIX

    stub = run_env(_config(syd_binary, sandbox_type="syd"))
    run_cmd.run(agent="claude", workspace=tmp_path, detach=True, sandbox="none")
    assert stub.run_kwargs is not None
    assert stub.run_kwargs["command"] == ["claude"]


def test_run_sandbox_missing_binary_fails_before_docker(
    run_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    run_env(_config(None))

    def _no_docker():  # type: ignore[no-untyped-def]
        raise AssertionError("DockerManager must not be created")

    monkeypatch.setattr(run_cmd, "DockerManager", _no_docker)
    with pytest.raises(run_cmd.typer.Exit) as exc:
        run_cmd.run(agent="claude", workspace=tmp_path, detach=True, sandbox="syd")
    assert exc.value.exit_code == 1
    assert "sandbox.syd.binary" in " ".join(capsys.readouterr().out.split())


def test_run_sandbox_keeps_init_entrypoint_outside_syd(
    run_env, tmp_path: Path, syd_binary: Path
) -> None:
    stub = run_env(_config(syd_binary, init=["echo hi"]))
    run_cmd.run(agent="claude", workspace=tmp_path, detach=True, sandbox="syd")
    assert stub.run_kwargs is not None
    assert stub.run_kwargs["command"] == ["/entrypoint.sh", *_SYD_PREFIX, "claude"]


def test_run_sandbox_makes_image_cmd_explicit(run_env, tmp_path: Path, syd_binary: Path) -> None:
    stub = run_env(_config(syd_binary), _Manager(image_cmd=["vibe"]))
    run_cmd.run(agent="vibe", workspace=tmp_path, detach=True, sandbox="syd")
    assert stub.run_kwargs is not None
    assert stub.run_kwargs["command"] == [*_SYD_PREFIX, "vibe"]


def test_task_sandbox_wraps_headless_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, syd_binary: Path
) -> None:
    stub = _Manager()
    monkeypatch.setattr(task_cmd, "is_dir_allowed", lambda p: True)
    monkeypatch.setattr(task_cmd, "_start_timeout_watcher", lambda task_id, timeout_seconds: None)
    monkeypatch.setattr(task_cmd, "_task_store", lambda: TaskStore(tmp_path / "tasks.db"))
    monkeypatch.setattr(task_cmd, "get_config", lambda: _config(syd_binary))
    monkeypatch.setattr(task_cmd, "DockerManager", lambda: stub)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    task_cmd.task_create(agent="claude", prompt="go", workspace=workspace, sandbox="syd")

    assert stub.run_kwargs is not None
    assert stub.run_kwargs["command"] == [*_SYD_PREFIX, "claude", "-p", "go"]
    assert SYD_POLICY_MOUNT in _volume_targets(stub.run_kwargs)
    assert stub.run_kwargs["extra_labels"][SANDBOX_LABEL] == "syd"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_run_rejects_unknown_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, syd_binary: Path
) -> None:
    monkeypatch.setattr(run_cmd, "get_config", lambda: _config(syd_binary))
    result = CliRunner().invoke(app, ["run", "claude", "-w", str(tmp_path), "--sandbox", "x"])
    assert result.exit_code == 1
    assert "Unknown sandbox type" in result.output


def test_cli_sandbox_policy_prints_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    result = CliRunner().invoke(app, ["sandbox", "policy", "hermes"])
    assert result.exit_code == 0, result.output
    assert "# Agent: hermes" in result.output
    assert "allow/wpath,cpath,dpath,tpath,fattr,symlink+/opt/data/***" in result.output


def test_cli_sandbox_check_reports_missing_binary() -> None:
    result = CliRunner().invoke(app, ["sandbox", "check"])
    assert result.exit_code == 1
    assert "sandbox.syd.binary" in result.output


def test_cli_sandbox_check_accepts_static_binary(
    monkeypatch: pytest.MonkeyPatch, syd_binary: Path
) -> None:
    monkeypatch.setenv("VP_SANDBOX_SYD_BINARY", str(syd_binary))
    result = CliRunner().invoke(app, ["sandbox", "check"])
    assert result.exit_code == 0, result.output
    assert "static, x86_64" in result.output


# ---------------------------------------------------------------------------
# Real syd (skipped unless VP_TEST_SYD_BINARY points at a syd binary)
# ---------------------------------------------------------------------------

_REAL_SYD = os.environ.get("VP_TEST_SYD_BINARY", "")


@pytest.mark.skipif(not _REAL_SYD, reason="set VP_TEST_SYD_BINARY to a syd binary")
def test_real_syd_enforces_rendered_policy(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is required")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    policy = write_policy(
        render_syd_policy(agent="claude", workspace_paths=[str(workspace)], writable_paths=[]),
        tmp_path / "cfg",
    )
    script = (
        "echo ok > f && echo ws-ok; "
        "echo x > .git/hooks/pre-commit 2>/dev/null && echo HOOK || echo hook-denied; "
        "echo x >> .git/config 2>/dev/null && echo CONFIG || echo config-denied; "
        'echo "key=$VP_SANDBOX_TEST_KEY"'
    )
    result = subprocess.run(
        [_REAL_SYD, "-P", str(policy), "--", "/bin/sh", "-c", script],
        cwd=workspace,
        capture_output=True,
        text=True,
        env={**os.environ, "VP_SANDBOX_TEST_KEY": "kept"},
        timeout=60,
    )
    assert result.stdout.split() == ["ws-ok", "hook-denied", "config-denied", "key=kept"], (
        result.stderr
    )
    assert '"act":"deny"' in result.stderr
