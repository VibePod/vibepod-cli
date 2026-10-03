"""microsandbox runtime: config validation, msb argv mapping, and CLI flows.

The CLI flows run the real `vp` commands against a stub `msb` executable that
records every invocation (argv plus the --runtime-conf it was handed).
"""

from __future__ import annotations

import json
import os
import stat
import sys
import types
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from vibepod.cli import app
from vibepod.commands import run
from vibepod.core import microsandbox as msb
from vibepod.core.config import get_config

pytestmark = pytest.mark.skipif(os.name == "nt", reason="stub msb is a POSIX script")

runner = CliRunner()

_FAKE_MSB = """#!{python}
import json, os, sys
argv = sys.argv[1:]
record = {{"argv": argv}}
if "--runtime-conf" in argv:
    with open(argv[argv.index("--runtime-conf") + 1], encoding="utf-8") as handle:
        record["conf"] = handle.read()
with open(os.environ["FAKE_MSB_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(record) + "\\n")
if argv[:1] == ["--version"]:
    print("msb 0.7.0")
elif argv[:1] == ["doctor"]:
    print("all checks passed")
sys.exit(int(os.environ.get("FAKE_MSB_EXIT", "0")))
"""


@pytest.fixture()
def fake_msb(monkeypatch, tmp_path: Path) -> dict[str, Any]:
    """Put a recording `msb` stub on PATH and make the host look KVM-capable."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "msb"
    binary.write_text(_FAKE_MSB.format(python=sys.executable), encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "msb-calls.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_MSB_LOG", str(log))
    monkeypatch.setattr(msb, "host_support_problem", lambda: None)

    def calls() -> list[dict[str, Any]]:
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    return {"binary": str(binary), "calls": calls}


@pytest.fixture()
def project(monkeypatch, tmp_path: Path) -> Path:
    """A hermetic config dir plus an allowed workspace as the cwd."""
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("VP_RUNTIME", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(run, "is_dir_allowed", lambda _: True)
    monkeypatch.setattr(run, "_reexec_with_herdr_hint", lambda *a, **k: None)

    def no_docker() -> None:
        raise AssertionError("the microsandbox runtime must not touch Docker")

    monkeypatch.setattr(run, "DockerManager", no_docker)
    return workspace


def _write_project_config(workspace: Path, data: dict[str, Any]) -> None:
    path = workspace / ".vibepod" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"version": 1, **data}), encoding="utf-8")


def _pairs(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == flag]


# --- config ---------------------------------------------------------------


def test_runtime_defaults_to_docker(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VP_RUNTIME", raising=False)
    config = get_config()
    assert config["runtime"] == "docker"
    assert config["microsandbox"] == {"binary": "msb", "cpus": None, "memory": None, "args": []}
    assert msb.resolve_runtime(config) == "docker"


def test_runtime_env_and_flag_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VP_RUNTIME", "microsandbox")
    config = get_config()
    assert msb.resolve_runtime(config) == "microsandbox"
    assert msb.resolve_runtime(config, "Docker") == "docker"


@pytest.mark.parametrize(
    ("config", "override", "message"),
    [
        ({"runtime": "firecracker"}, None, "Invalid runtime value 'firecracker'"),
        ({}, "kata", "Invalid --runtime value 'kata'"),
    ],
)
def test_resolve_runtime_rejects_unknown(config, override, message) -> None:
    with pytest.raises(ValueError, match=message):
        msb.resolve_runtime(config, override)


def test_load_settings_normalizes_values() -> None:
    settings = msb.load_settings(
        {"microsandbox": {"binary": "~/bin/msb", "cpus": "4", "memory": 1024, "args": "--no-net"}},
    )
    assert settings == msb.MicrosandboxSettings(
        binary="~/bin/msb",
        cpus=4,
        memory="1024M",
        args=["--no-net"],
    )
    assert msb.load_settings({}) == msb.MicrosandboxSettings()


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ("msb", "expected a mapping"),
        ({"cpu": 2}, "Unknown microsandbox config key"),
        ({"cpus": 0}, "microsandbox.cpus"),
        ({"cpus": "two"}, "microsandbox.cpus"),
        ({"cpus": True}, "microsandbox.cpus"),
        ({"memory": "lots"}, "microsandbox.memory"),
        ({"memory": -1}, "microsandbox.memory"),
        ({"args": [1]}, "microsandbox.args"),
        ({"binary": ["msb"]}, "microsandbox.binary"),
    ],
)
def test_load_settings_rejects_invalid(section, message) -> None:
    with pytest.raises(ValueError, match=message):
        msb.load_settings({"microsandbox": section})


# --- host diagnostics -------------------------------------------------------


def test_find_binary_missing_points_at_install(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(msb.MicrosandboxError, match="install.microsandbox.dev"):
        msb.find_binary(msb.MicrosandboxSettings())


def test_find_binary_rejects_non_executable_path(tmp_path: Path) -> None:
    target = tmp_path / "msb"
    target.write_text("", encoding="utf-8")
    with pytest.raises(msb.MicrosandboxError, match="not an executable file"):
        msb.find_binary(msb.MicrosandboxSettings(binary=str(target)))


def test_find_binary_uses_installer_location(monkeypatch, tmp_path: Path) -> None:
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    binary = local_bin / "msb"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert msb.find_binary(msb.MicrosandboxSettings()) == str(binary)


def test_host_support_rejects_intel_mac(monkeypatch) -> None:
    monkeypatch.setattr(msb.sys, "platform", "darwin")
    monkeypatch.setattr(msb.platform, "machine", lambda: "x86_64")
    assert "Apple Silicon" in (msb.host_support_problem() or "")
    monkeypatch.setattr(msb.platform, "machine", lambda: "arm64")
    assert msb.host_support_problem() is None


def test_ensure_available_reports_host_problem(monkeypatch, fake_msb) -> None:
    monkeypatch.setattr(msb, "host_support_problem", lambda: "/dev/kvm is missing")
    with pytest.raises(msb.MicrosandboxError, match="/dev/kvm is missing.*msb doctor"):
        msb.ensure_available(msb.MicrosandboxSettings())


# --- argv mapping ---------------------------------------------------------------


def test_port_args_maps_docker_bindings() -> None:
    bindings = {"3081/tcp": [("127.0.0.1", "3080")], "8000": ["8000"], "1456/tcp": 1455}
    assert msb.port_args(bindings) == [
        "-p",
        "127.0.0.1:3080:3081",
        "-p",
        "8000:8000",
        "-p",
        "1455:1456",
    ]


@pytest.mark.parametrize(
    ("bindings", "message"),
    [
        ({"6000/udp": ["6000"]}, "TCP ports only"),
        ({"80": ["0"]}, "explicit host port"),
        ({"90": [("127.0.0.1", None)]}, "explicit host port"),
    ],
)
def test_port_args_rejects_unsupported(bindings, message) -> None:
    with pytest.raises(msb.MicrosandboxError, match=message):
        msb.port_args(bindings)


def test_mount_args_picks_mount_kind(tmp_path: Path) -> None:
    directory = tmp_path / "data"
    directory.mkdir()
    file = tmp_path / "bootstrap.cjs"
    file.write_text("", encoding="utf-8")
    assert msb.mount_args(
        [
            (str(directory), "/data", "rw"),
            (str(file), "/opt/bootstrap.cjs", "ro"),
            ("cache", "/cache", "ro,Z"),
        ],
    ) == [
        "-v",
        f"{directory}:/data",
        "--mount-file",
        f"{file}:/opt/bootstrap.cjs:ro",
        "--mount-named",
        "cache:/cache:ro",
    ]


def test_runtime_conf_is_private_and_quoted() -> None:
    path = msb.write_runtime_conf(
        env={"ANTHROPIC_API_KEY": "secret", "FLAG": "yes", "PORT": "8080"},
        labels={"vibepod.managed": "true"},
        workdir="/workspace",
    )
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        text = path.read_text(encoding="utf-8")
        assert '"FLAG": "yes"' in text
        assert yaml.safe_load(text) == {
            "workdir": "/workspace",
            "env": {"ANTHROPIC_API_KEY": "secret", "FLAG": "yes", "PORT": "8080"},
            "labels": {"vibepod.managed": "true"},
        }
    finally:
        path.unlink()


def test_build_run_argv_layout(tmp_path: Path) -> None:
    argv = msb.build_run_argv(
        binary="msb",
        settings=msb.MicrosandboxSettings(cpus=2, memory="2G", args=["--net", "public"]),
        image="vibepod/claude:latest",
        runtime_conf=tmp_path / "conf.yaml",
        volumes=[(str(tmp_path), "/workspace", "rw")],
        ports=None,
        command=["claude", "--help"],
        name="box",
        detach=True,
    )
    assert argv == [
        "msb",
        "run",
        "--runtime-conf",
        str(tmp_path / "conf.yaml"),
        "--name",
        "box",
        "--detach",
        "--cpus",
        "2",
        "--memory",
        "2G",
        "-v",
        f"{tmp_path}:/workspace",
        "--net",
        "public",
        "vibepod/claude:latest",
        "--",
        "claude",
        "--help",
    ]


# --- vp run ---------------------------------------------------------------------


def test_run_launches_msb_with_workspace_env_and_args(fake_msb, project: Path) -> None:
    _write_project_config(
        project,
        {
            "runtime": "microsandbox",
            "auto_pull": False,
            "microsandbox": {"cpus": 2, "memory": "2G"},
        },
    )
    result = runner.invoke(
        app,
        ["run", "claude", "-e", "ANTHROPIC_API_KEY=sk-secret", "--ikwid", "--", "--verbose"],
    )
    assert result.exit_code == 0, result.output

    calls = fake_msb["calls"]()
    assert len(calls) == 1
    argv = calls[0]["argv"]
    assert argv[0] == "run"
    assert "sk-secret" not in " ".join(argv)
    assert "--name" not in argv  # ephemeral: msb removes it on exit
    assert _pairs(argv, "--cpus") == ["2"]
    assert _pairs(argv, "--memory") == ["2G"]
    mounts = _pairs(argv, "-v")
    assert f"{project}:/workspace" in mounts
    assert any(mount.endswith(":/claude") for mount in mounts)
    separator = argv.index("--")
    assert argv[separator - 1] == "vibepod/claude:latest"
    assert argv[separator + 1 :] == ["claude", "--dangerously-skip-permissions", "--verbose"]

    conf = yaml.safe_load(calls[0]["conf"])
    assert conf["workdir"] == "/workspace"
    assert conf["env"]["ANTHROPIC_API_KEY"] == "sk-secret"
    assert conf["env"]["CLAUDE_CONFIG_DIR"] == "/claude"
    assert conf["labels"]["vibepod.managed"] == "true"
    assert conf["labels"]["vibepod.runtime"] == "microsandbox"
    # The secret-bearing config file is gone once msb returns.
    conf_path = Path(argv[argv.index("--runtime-conf") + 1])
    assert not conf_path.exists()


def test_run_runtime_flag_overrides_config(fake_msb, project: Path) -> None:
    _write_project_config(project, {"auto_pull": False})
    result = runner.invoke(app, ["run", "codex", "--runtime", "microsandbox"])
    assert result.exit_code == 0, result.output
    assert fake_msb["calls"]()[0]["argv"][-2:] == ["--", "codex"]


def test_run_alias_accepts_runtime_flag(fake_msb, project: Path) -> None:
    _write_project_config(project, {"auto_pull": False})
    result = runner.invoke(app, ["c", "--runtime", "microsandbox"])
    assert result.exit_code == 0, result.output
    assert fake_msb["calls"]()[0]["argv"][-2:] == ["--", "claude"]


def test_run_detach_names_the_sandbox(fake_msb, project: Path) -> None:
    _write_project_config(project, {"runtime": "microsandbox", "auto_pull": False})
    result = runner.invoke(app, ["run", "claude", "-d", "-p", "127.0.0.1:9000:9000"])
    assert result.exit_code == 0, result.output
    argv = fake_msb["calls"]()[0]["argv"]
    assert "--detach" in argv
    name = _pairs(argv, "--name")[0]
    assert name.startswith("vibepod-claude-")
    assert _pairs(argv, "-p") == ["127.0.0.1:9000:9000"]
    assert name in result.output


def test_run_named_sandbox_is_removed_after_exit(fake_msb, project: Path) -> None:
    _write_project_config(project, {"runtime": "microsandbox", "auto_pull": False})
    result = runner.invoke(app, ["run", "claude", "--name", "mybox"])
    assert result.exit_code == 0, result.output
    argvs = [call["argv"] for call in fake_msb["calls"]()]
    assert _pairs(argvs[0], "--name") == ["mybox"]
    assert argvs[1] == ["rm", "mybox"]


def test_run_pull_refreshes_image_first(fake_msb, project: Path) -> None:
    _write_project_config(project, {"runtime": "microsandbox"})
    result = runner.invoke(app, ["run", "claude", "--pull"])
    assert result.exit_code == 0, result.output
    argvs = [call["argv"] for call in fake_msb["calls"]()]
    assert argvs[0] == ["pull", "vibepod/claude:latest"]
    assert argvs[1][0] == "run"


def test_run_propagates_msb_exit_code(monkeypatch, fake_msb, project: Path) -> None:
    _write_project_config(project, {"runtime": "microsandbox", "auto_pull": False})
    monkeypatch.setenv("FAKE_MSB_EXIT", "7")
    result = runner.invoke(app, ["run", "claude"])
    assert result.exit_code == 7


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--acp"], "--acp cannot be used with the microsandbox runtime"),
        (["--network", "dev"], "--network cannot be used with the microsandbox runtime"),
        (["--paste-images"], "--paste-images cannot be used"),
    ],
)
def test_run_rejects_docker_only_flags(fake_msb, project: Path, args, message) -> None:
    _write_project_config(project, {"runtime": "microsandbox"})
    result = runner.invoke(app, ["run", "claude", *args])
    assert result.exit_code == 1
    assert message in " ".join(result.output.split())
    assert fake_msb["calls"]() == []


def test_run_reports_missing_msb(monkeypatch, project: Path, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_project_config(project, {"runtime": "microsandbox"})
    result = runner.invoke(app, ["run", "claude"])
    assert result.exit_code == 3
    assert "not found on PATH" in result.output


def test_run_reports_invalid_config(fake_msb, project: Path) -> None:
    _write_project_config(project, {"runtime": "microsandbox", "microsandbox": {"cpus": -1}})
    result = runner.invoke(app, ["run", "claude"])
    assert result.exit_code == 1
    assert "microsandbox.cpus" in result.output


def test_run_rejects_unknown_runtime(project: Path) -> None:
    result = runner.invoke(app, ["run", "claude", "--runtime", "lxc"])
    assert result.exit_code == 1
    assert "Invalid --runtime value 'lxc'" in result.output


def test_run_rejects_init_commands(fake_msb, project: Path) -> None:
    _write_project_config(
        project,
        {"runtime": "microsandbox", "agents": {"claude": {"init": ["apt-get update"]}}},
    )
    result = runner.invoke(app, ["run", "claude"])
    assert result.exit_code == 1
    assert "agents.claude.init is not supported" in " ".join(result.output.split())
    assert fake_msb["calls"]() == []


def test_run_rejects_overlay_unless_skipped(fake_msb, project: Path) -> None:
    _write_project_config(project, {"runtime": "microsandbox", "auto_pull": False})
    overlay = project / ".vibepod" / "overlay" / "Dockerfile"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("FROM scratch\n", encoding="utf-8")

    result = runner.invoke(app, ["run", "claude"])
    assert result.exit_code == 1
    assert "--no-overlay" in result.output

    result = runner.invoke(app, ["run", "claude", "--no-overlay"])
    assert result.exit_code == 0, result.output


def test_run_docker_runtime_is_unchanged(monkeypatch, fake_msb, project: Path) -> None:
    """Without the runtime switch, vp run still goes through Docker."""
    reached: list[bool] = []

    def docker_manager() -> None:
        reached.append(True)
        raise run.DockerClientError("docker down")

    monkeypatch.setattr(run, "DockerManager", docker_manager)
    result = runner.invoke(app, ["run", "claude"])
    assert result.exit_code == 3
    assert reached == [True]
    assert fake_msb["calls"]() == []


def test_task_refuses_microsandbox_runtime(fake_msb, project: Path) -> None:
    _write_project_config(project, {"runtime": "microsandbox"})
    result = runner.invoke(app, ["task", "create", "claude", "hello"])
    assert result.exit_code == 1
    assert "VP_RUNTIME=docker" in result.output


# --- vp config init ----------------------------------------------------------------


def _project_config(workspace: Path) -> dict[str, Any]:
    return yaml.safe_load((workspace / ".vibepod" / "config.yaml").read_text(encoding="utf-8"))


def test_config_init_with_microsandbox(fake_msb, project: Path) -> None:
    result = runner.invoke(
        app,
        ["config", "init", "--runtime", "microsandbox", "--msb-cpus", "2", "--msb-memory", "4G"],
    )
    assert result.exit_code == 0, result.output
    assert _project_config(project) == {
        "version": 1,
        "runtime": "microsandbox",
        "microsandbox": {"cpus": 2, "memory": "4G"},
    }
    assert "not ready" not in result.output
    assert msb.resolve_runtime(get_config()) == "microsandbox"


def test_config_init_updates_existing_project(fake_msb, project: Path) -> None:
    _write_project_config(project, {"default_agent": "codex"})
    result = runner.invoke(app, ["config", "init", "--runtime", "microsandbox"])
    assert result.exit_code == 0, result.output
    data = _project_config(project)
    assert data["default_agent"] == "codex"
    assert data["runtime"] == "microsandbox"


def test_config_init_with_agent_and_runtime(fake_msb, project: Path) -> None:
    result = runner.invoke(app, ["config", "init", "claude", "--runtime", "microsandbox"])
    assert result.exit_code == 0, result.output
    data = _project_config(project)
    assert "claude" in data["agents"]
    assert data["runtime"] == "microsandbox"


def test_config_init_warns_when_host_not_ready(monkeypatch, project: Path, tmp_path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    monkeypatch.setenv("HOME", str(tmp_path))
    result = runner.invoke(app, ["config", "init", "--runtime", "microsandbox"])
    # CI can write the config before msb is installed.
    assert result.exit_code == 0
    assert "not ready on this host" in result.output
    assert "vp doctor microsandbox" in result.output


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--msb-cpus", "2"], "require --runtime microsandbox"),
        (["--runtime", "docker", "--msb-memory", "2G"], "require --runtime microsandbox"),
        (["--runtime", "vm"], "Invalid --runtime value 'vm'"),
        (["--runtime", "microsandbox", "--msb-memory", "huge"], "--msb-memory"),
        (["--runtime", "microsandbox", "--msb-cpus", "0"], "--msb-cpus"),
    ],
)
def test_config_init_rejects_invalid_runtime_options(project: Path, args, message) -> None:
    result = runner.invoke(app, ["config", "init", *args])
    assert result.exit_code == 1
    assert message in result.output
    assert not (project / ".vibepod" / "config.yaml").exists()


def test_config_init_docker_runtime(project: Path) -> None:
    result = runner.invoke(app, ["config", "init", "--runtime", "docker"])
    assert result.exit_code == 0, result.output
    assert _project_config(project) == {"version": 1, "runtime": "docker"}


def test_config_init_interactive(monkeypatch, fake_msb, project: Path) -> None:
    from vibepod.commands import config as config_cmd

    tty = types.SimpleNamespace(stdin=types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(config_cmd, "sys", tty)
    result = runner.invoke(app, ["config", "init", "-i"], input="microsandbox\nx\n4\n8G\n")
    assert result.exit_code == 0, result.output
    assert _project_config(project) == {
        "version": 1,
        "runtime": "microsandbox",
        "microsandbox": {"cpus": 4, "memory": "8G"},
    }


def test_config_init_interactive_needs_tty(project: Path) -> None:
    result = runner.invoke(app, ["config", "init", "-i"])
    assert result.exit_code == 1
    assert "--interactive needs a terminal" in result.output


# --- vp doctor microsandbox ----------------------------------------------------


def test_doctor_reports_ready(fake_msb, project: Path) -> None:
    _write_project_config(project, {"runtime": "microsandbox"})
    result = runner.invoke(app, ["doctor", "microsandbox"])
    assert result.exit_code == 0, result.output
    assert "msb 0.7.0" in result.output
    assert "all checks passed" in result.output
    assert "ready" in result.output
    assert [call["argv"] for call in fake_msb["calls"]()] == [["--version"], ["doctor"]]


def test_doctor_fails_when_msb_doctor_fails(monkeypatch, fake_msb, project: Path) -> None:
    monkeypatch.setenv("FAKE_MSB_EXIT", "1")
    result = runner.invoke(app, ["doctor", "microsandbox"])
    assert result.exit_code == 1
    assert "msb doctor` reported problems" in result.output


def test_doctor_fails_without_kvm_or_binary(monkeypatch, project: Path, tmp_path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(msb, "host_support_problem", lambda: "/dev/kvm is missing")
    result = runner.invoke(app, ["doctor", "microsandbox"])
    assert result.exit_code == 1
    assert "/dev/kvm is missing" in result.output
    assert "2 problem(s) found" in result.output
