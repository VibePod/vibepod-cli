"""Tests for the opt-in gVisor (runsc) agent sandbox."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from vibepod.cli import app
from vibepod.commands import doctor as doctor_cmd
from vibepod.core import sandbox
from vibepod.core.config import _apply_env, _default_config
from vibepod.core.sandbox import (
    NO_SANDBOX,
    SandboxError,
    SandboxSettings,
    prepare_sandbox,
    resolve_sandbox,
    runtime_flag,
)

GVISOR = SandboxSettings(mode="gvisor")


class _Engine:
    """Minimal engine stub exposing the probes prepare_sandbox uses."""

    def __init__(
        self,
        *,
        runtimes: dict[str, dict[str, Any]] | None = None,
        podman: bool = False,
        rootless: bool = False,
        selinux: bool = False,
    ) -> None:
        self._runtimes = runtimes if runtimes is not None else {"runc": {"path": "runc"}}
        self._podman = podman
        self._rootless = rootless
        self._selinux = selinux

    def runtimes(self) -> dict[str, dict[str, Any]]:
        return self._runtimes

    def is_podman(self) -> bool:
        return self._podman

    def is_rootless_engine(self) -> bool:
        return self._rootless

    def selinux_enabled(self) -> bool:
        return self._selinux


def _runsc(*args: str, name: str = "runsc") -> dict[str, dict[str, Any]]:
    entry: dict[str, Any] = {"path": "/usr/bin/runsc"}
    if args:
        entry["runtimeArgs"] = list(args)
    return {"runc": {"path": "runc"}, name: entry}


@pytest.fixture(autouse=True)
def _linux(monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.delenv("VP_SANDBOX", raising=False)


def _write_project(workspace: Path, body: str) -> None:
    (workspace / ".vibepod").mkdir(parents=True, exist_ok=True)
    (workspace / ".vibepod" / "config.yaml").write_text(body, encoding="utf-8")


# --- resolve_sandbox ---------------------------------------------------------


def test_default_config_disables_sandbox(tmp_path) -> None:
    config = _default_config()
    assert config["sandbox"] == {
        "type": "none",
        "gvisor": {"runtime": "runsc"},
        "microsandbox": {"binary": "msb", "cpus": None, "memory": None, "args": []},
    }
    assert resolve_sandbox(config, tmp_path) == SandboxSettings(mode="none", runtime="runsc")


def test_resolve_reads_config_mapping_and_runtime(tmp_path) -> None:
    config = {"sandbox": {"mode": "gvisor", "gvisor": {"runtime": "runsc-vibepod"}}}
    assert resolve_sandbox(config, tmp_path) == SandboxSettings("gvisor", "runsc-vibepod")


def test_resolve_accepts_string_shorthand(tmp_path) -> None:
    assert resolve_sandbox({"sandbox": "gvisor"}, tmp_path).mode == "gvisor"


def test_launched_workspace_project_config_wins_over_merged_config(tmp_path) -> None:
    _write_project(tmp_path, "sandbox:\n  mode: gvisor\n")
    assert resolve_sandbox({"sandbox": {"mode": "none"}}, tmp_path).mode == "gvisor"


def test_env_overrides_project_and_cli_overrides_env(monkeypatch, tmp_path) -> None:
    _write_project(tmp_path, "sandbox: gvisor\n")
    monkeypatch.setenv("VP_SANDBOX", "none")
    assert resolve_sandbox({}, tmp_path).mode == "none"
    assert resolve_sandbox({}, tmp_path, "gvisor").mode == "gvisor"
    monkeypatch.setenv("VP_SANDBOX", "gvisor")
    assert resolve_sandbox({}, tmp_path, "none").mode == "none"


def test_env_selection_is_resolved_separately(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("VP_SANDBOX", "gvisor")
    config = _apply_env(_default_config())
    assert config["sandbox"]["type"] == "none"
    assert resolve_sandbox(config, tmp_path).type == "gvisor"


@pytest.mark.parametrize("value", ["firecracker", "runsc", "yes"])
def test_resolve_rejects_unknown_mode(tmp_path, value) -> None:
    with pytest.raises(SandboxError, match="expected one of: none, gvisor"):
        resolve_sandbox({}, tmp_path, value)


def test_resolve_rejects_non_mapping_section(tmp_path) -> None:
    with pytest.raises(SandboxError, match="must be a mapping"):
        resolve_sandbox({"sandbox": ["gvisor"]}, tmp_path)


# --- runtime_flag -------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--network=host"], "host"),
        (["--network", "host"], "host"),
        (["-network=none"], "none"),
        (["--platform=kvm", "--host-uds=open"], None),
        ([], None),
        (["network=host"], None),
    ],
)
def test_runtime_flag(args, expected) -> None:
    assert runtime_flag(args, "network") == expected


# --- prepare_sandbox ----------------------------------------------------------


def test_prepare_none_touches_no_engine() -> None:
    assert prepare_sandbox(object(), SandboxSettings()) is NO_SANDBOX
    assert NO_SANDBOX.runtime is None
    assert NO_SANDBOX.labels == {}


def test_prepare_rejects_non_linux_host(monkeypatch) -> None:
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    with pytest.raises(SandboxError, match="needs a Linux host"):
        prepare_sandbox(_Engine(runtimes=_runsc("--network=host")), GVISOR)


def test_prepare_rejects_rootless_engine() -> None:
    engine = _Engine(runtimes=_runsc("--network=host"), rootless=True)
    with pytest.raises(SandboxError, match="rootless"):
        prepare_sandbox(engine, GVISOR)


def test_prepare_reports_unregistered_docker_runtime() -> None:
    with pytest.raises(SandboxError) as exc:
        prepare_sandbox(_Engine(), GVISOR)
    message = str(exc.value)
    assert "'runsc' is not registered with Docker" in message
    assert "registered: runc" in message
    assert "runsc install --runtime runsc -- --network=host --host-uds=open" in message


def test_prepare_rejects_docker_runtime_on_gvisor_netstack() -> None:
    with pytest.raises(SandboxError) as exc:
        prepare_sandbox(_Engine(runtimes=_runsc("--platform=systrap")), GVISOR)
    assert "google/gvisor#7469" in str(exc.value)
    assert "Re-register it" in str(exc.value)
    assert "runsc-vibepod" in str(exc.value)


def test_prepare_docker_with_host_network_runtime() -> None:
    plan = prepare_sandbox(_Engine(runtimes=_runsc("--network=host")), GVISOR)
    assert plan.runtime == "runsc"
    assert plan.labels == {"vibepod.sandbox": "gvisor"}
    assert plan.security_opt == []
    assert plan.host_socket_mounts is False
    assert plan.socket_skip_reason is not None and "--host-uds=open" in plan.socket_skip_reason
    assert plan.suffix == " in the gvisor sandbox (runtime runsc)"


def test_prepare_custom_runtime_with_host_uds_keeps_socket_mounts() -> None:
    runtimes = _runsc("--network=host", "--host-uds=open", name="runsc-vibepod")
    plan = prepare_sandbox(_Engine(runtimes=runtimes), SandboxSettings("gvisor", "runsc-vibepod"))
    assert plan.runtime == "runsc-vibepod"
    assert plan.host_socket_mounts is True
    assert plan.socket_skip_reason is None


def test_prepare_shim_runtime_warns_instead_of_guessing_flags() -> None:
    runtimes = {"runsc": {"runtimeType": "io.containerd.runsc.v1"}}
    plan = prepare_sandbox(_Engine(runtimes=runtimes), GVISOR)
    assert plan.runtime == "runsc"
    assert plan.notes and "network=host" in plan.notes[0]


def test_prepare_podman_does_not_trust_runtime_listing() -> None:
    # Podman lists runsc from containers.conf whether or not it is installed;
    # its absence must not block (create reports it), nor its missing args.
    plan = prepare_sandbox(_Engine(runtimes={}, podman=True), GVISOR)
    assert plan.runtime == "runsc"
    assert plan.host_socket_mounts is False


def test_prepare_disables_selinux_labels() -> None:
    engine = _Engine(runtimes={}, podman=True, selinux=True)
    assert prepare_sandbox(engine, GVISOR).security_opt == ["label=disable"]


# --- vp doctor sandbox --------------------------------------------------------


def test_doctor_sandbox_reports_ready_engine(monkeypatch, tmp_path) -> None:
    from vibepod.core import docker as docker_mod

    engine = _Engine(runtimes=_runsc("--network=host", "--host-uds=open"))
    monkeypatch.setattr(docker_mod, "DockerManager", lambda: engine)
    monkeypatch.setattr(doctor_cmd, "get_config", lambda: {"sandbox": {"mode": "gvisor"}})

    result = CliRunner().invoke(app, ["doctor", "sandbox", "-w", str(tmp_path)])

    output = " ".join(result.output.split())
    assert result.exit_code == 0, result.output
    assert "configured sandbox: gvisor" in output
    assert "--network=host --host-uds=open" in output
    assert "prerequisites look good" in output


def test_doctor_sandbox_fails_without_runtime(monkeypatch, tmp_path) -> None:
    from vibepod.core import docker as docker_mod

    monkeypatch.setattr(docker_mod, "DockerManager", lambda: _Engine())
    monkeypatch.setattr(doctor_cmd, "get_config", lambda: {})

    result = CliRunner().invoke(app, ["doctor", "sandbox", "gvisor", "-w", str(tmp_path)])

    output = " ".join(result.output.split())
    assert result.exit_code == 1
    assert "configured sandbox: gvisor" in output
    assert "not registered with Docker" in output


def test_canonical_config_overrides_legacy_in_same_layer(monkeypatch, tmp_path):
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path / "global"))
    monkeypatch.chdir(tmp_path)
    _write_project(tmp_path, "runtime: microsandbox\nsandbox:\n  type: gvisor\n  mode: none\n")
    from vibepod.core.config import get_config

    config = get_config()
    assert config["sandbox"]["type"] == "gvisor"
    assert "mode" not in config["sandbox"]
    assert "runtime" not in config
    assert resolve_sandbox(config, tmp_path).type == "gvisor"


def test_selection_precedence_across_aliases(monkeypatch, tmp_path):
    _write_project(tmp_path, "sandbox: microsandbox\n")
    monkeypatch.setenv("VP_RUNTIME", "docker")
    assert resolve_sandbox({"sandbox": "gvisor"}, tmp_path).type == "none"
    monkeypatch.setenv("VP_SANDBOX", "gvisor")
    assert resolve_sandbox({}, tmp_path).type == "gvisor"
    assert resolve_sandbox({}, tmp_path, legacy_runtime="microsandbox").type == "microsandbox"
    assert resolve_sandbox({}, tmp_path, "none", "microsandbox").type == "none"


def test_version_reports_selected_sandbox_json(monkeypatch, tmp_path):
    import json

    from vibepod.commands import update

    monkeypatch.setattr(update, "_runtime_info", lambda: ("Docker", "test"))
    monkeypatch.setattr(update, "get_config", lambda: {"sandbox": {"type": "microsandbox"}})
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["version", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["sandbox"] == "microsandbox"


def test_version_json_keeps_legacy_warning_on_stderr(monkeypatch, tmp_path):
    import json

    from vibepod.commands import update

    monkeypatch.setattr(update, "_runtime_info", lambda: (None, "unavailable"))
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path / "global"))
    monkeypatch.chdir(tmp_path)
    _write_project(tmp_path, "runtime: microsandbox\n")
    result = CliRunner().invoke(app, ["version", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["sandbox"] == "microsandbox"
    assert result.stderr.count("runtime is deprecated") == 1


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("source", ["config", "VP_SANDBOX", "VP_RUNTIME"])
def test_version_invalid_sandbox_is_clean_error(monkeypatch, tmp_path, as_json, source):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VP_SANDBOX", raising=False)
    monkeypatch.delenv("VP_RUNTIME", raising=False)
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path / "global"))
    if source == "config":
        _write_project(tmp_path, "sandbox: foo\n")
    if source != "config":
        monkeypatch.setenv(source, "foo")
    result = CliRunner().invoke(app, ["version", *(["--json"] if as_json else [])])
    assert result.exit_code == 1
    assert "foo" in result.output
    assert source in result.output
    assert "Traceback" not in result.output


def test_invalid_legacy_runtime_config_warns_without_crashing(monkeypatch, tmp_path):
    from vibepod.core.config import get_config

    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path / "global"))
    monkeypatch.chdir(tmp_path)
    _write_project(tmp_path, "runtime: foo\n")
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 1
    assert "Invalid config runtime 'foo'" in result.output
    assert "Traceback" not in result.output
    assert get_config()["sandbox"]["type"] == "none"


def test_list_invalid_global_legacy_runtime_warns_and_continues(monkeypatch, tmp_path):
    from vibepod.commands import list_cmd

    root = tmp_path / "global"
    root.mkdir()
    (root / "config.yaml").write_text("runtime: foo\n")
    monkeypatch.setenv("VP_CONFIG_DIR", str(root))
    monkeypatch.chdir(tmp_path)

    class Manager:
        def list_managed(self, **kwargs):
            return []

    monkeypatch.setattr(list_cmd, "DockerManager", Manager)
    result = CliRunner().invoke(app, ["list"])
    assert result.exit_code == 0
    assert "Invalid runtime 'foo'" in result.output
    assert "Traceback" not in result.output
