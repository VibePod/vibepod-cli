"""CLI smoke tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from vibepod import compat as compat_module
from vibepod.cli import app
from vibepod.commands import run as run_cmd
from vibepod.compat import (
    install_python314_http_client_flush_patch,
    should_ignore_closed_http_response_flush_error,
)

runner = CliRunner()


def test_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "VibePod" in result.stdout


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "VibePod CLI" in result.stdout


def _fake_manager(version_info: dict) -> object:
    client = SimpleNamespace(version=lambda: version_info)
    return SimpleNamespace(client=client)


def test_version_reports_podman_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    from vibepod.commands import update as update_cmd

    version_info = {
        "Version": "4.9.3",
        "Components": [{"Name": "Podman Engine", "Version": "4.9.3"}],
    }
    monkeypatch.setattr(update_cmd, "DockerManager", lambda: _fake_manager(version_info))

    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "Runtime:     Podman 4.9.3" in result.stdout


def test_version_reports_docker_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    from vibepod.commands import update as update_cmd

    version_info = {
        "Version": "27.5.1",
        "Platform": {"Name": "Docker Engine - Community"},
    }
    monkeypatch.setattr(update_cmd, "DockerManager", lambda: _fake_manager(version_info))

    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "Runtime:     Docker 27.5.1" in result.stdout


def test_version_json_includes_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from vibepod.commands import update as update_cmd

    version_info = {
        "Version": "4.9.3",
        "Components": [{"Name": "Podman Engine", "Version": "4.9.3"}],
    }
    monkeypatch.setattr(update_cmd, "DockerManager", lambda: _fake_manager(version_info))

    result = runner.invoke(app, ["version", "--json"])
    assert result.exit_code == 0
    info = json.loads(result.stdout)
    assert info["runtime"] == "Podman"
    assert info["docker"] == "4.9.3"


def test_version_runtime_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    from vibepod.commands import update as update_cmd
    from vibepod.core.docker import DockerClientError

    def _raise() -> object:
        raise DockerClientError("no engine")

    monkeypatch.setattr(update_cmd, "DockerManager", _raise)

    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "Runtime:     unavailable" in result.stdout


def test_version_runtime_disconnects_after_init(monkeypatch: pytest.MonkeyPatch) -> None:
    from vibepod.commands import update as update_cmd
    from vibepod.core.docker import DockerException

    def _raise_version() -> dict:
        raise DockerException("connection aborted")

    client = SimpleNamespace(version=_raise_version)
    monkeypatch.setattr(update_cmd, "DockerManager", lambda: SimpleNamespace(client=client))

    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "Runtime:     unavailable" in result.stdout


def test_python314_http_response_flush_filter_matches_closed_fp_error() -> None:
    response = SimpleNamespace(fp=SimpleNamespace(closed=True))
    exc = ValueError("I/O operation on closed file.")

    assert should_ignore_closed_http_response_flush_error(response, exc) is True


def test_python314_http_response_flush_filter_does_not_hide_other_errors() -> None:
    closed_response = SimpleNamespace(fp=SimpleNamespace(closed=True))
    open_response = SimpleNamespace(fp=SimpleNamespace(closed=False))

    assert (
        should_ignore_closed_http_response_flush_error(
            closed_response,
            ValueError("different error"),
        )
        is False
    )
    assert (
        should_ignore_closed_http_response_flush_error(
            open_response,
            ValueError("I/O operation on closed file."),
        )
        is False
    )
    assert (
        should_ignore_closed_http_response_flush_error(
            closed_response,
            RuntimeError("I/O operation on closed file."),
        )
        is False
    )


def test_python314_http_response_flush_patch_suppresses_closed_fp_error(monkeypatch) -> None:
    def broken_flush(self) -> None:  # noqa: ANN001
        raise ValueError("I/O operation on closed file.")

    monkeypatch.setattr(compat_module.sys, "version_info", (3, 14))
    monkeypatch.setattr(compat_module.http.client.HTTPResponse, "flush", broken_flush)

    install_python314_http_client_flush_patch()

    response = SimpleNamespace(fp=SimpleNamespace(closed=True))
    compat_module.http.client.HTTPResponse.flush(response)  # type: ignore[arg-type]


def test_python314_http_response_flush_patch_reraises_non_matching_value_error(
    monkeypatch,
) -> None:
    def broken_flush(self) -> None:  # noqa: ANN001
        raise ValueError("I/O operation on closed file.")

    monkeypatch.setattr(compat_module.sys, "version_info", (3, 14))
    monkeypatch.setattr(compat_module.http.client.HTTPResponse, "flush", broken_flush)

    install_python314_http_client_flush_patch()

    response = SimpleNamespace(fp=SimpleNamespace(closed=False))
    with pytest.raises(ValueError, match="I/O operation on closed file"):
        compat_module.http.client.HTTPResponse.flush(response)  # type: ignore[arg-type]


def test_full_agent_name_alias_runs_agent(monkeypatch) -> None:
    called: dict[str, object] = {"agent": None, "passthrough": None}

    def _fake_run(agent=None, **kwargs) -> None:  # noqa: ANN001, ANN003, ARG001
        called["agent"] = agent
        called["passthrough"] = list(kwargs.get("passthrough_args") or [])

    monkeypatch.setattr(run_cmd, "run", _fake_run)

    result = runner.invoke(app, ["claude"])
    assert result.exit_code == 0
    assert called["agent"] == "claude"
    assert called["passthrough"] == []


def test_pi_alias_runs_agent(monkeypatch) -> None:
    called: dict[str, object] = {"agent": None, "passthrough": None}

    def _fake_run(agent=None, **kwargs) -> None:  # noqa: ANN001, ANN003, ARG001
        called["agent"] = agent
        called["passthrough"] = list(kwargs.get("passthrough_args") or [])

    monkeypatch.setattr(run_cmd, "run", _fake_run)

    result = runner.invoke(app, ["pi"])
    assert result.exit_code == 0
    assert called["agent"] == "pi"
    assert called["passthrough"] == []


def test_tau_shortcut_runs_tau(monkeypatch) -> None:
    called: dict[str, object] = {"agent": None, "passthrough": None}

    def _fake_run(agent=None, **kwargs) -> None:  # noqa: ANN001, ANN003, ARG001
        called["agent"] = agent
        called["passthrough"] = list(kwargs.get("passthrough_args") or [])

    monkeypatch.setattr(run_cmd, "run", _fake_run)

    result = runner.invoke(app, ["t"])
    assert result.exit_code == 0
    assert called["agent"] == "tau"
    assert called["passthrough"] == []


def test_jcode_shortcut_runs_jcode(monkeypatch) -> None:
    called: dict[str, object] = {"agent": None, "passthrough": None}

    def _fake_run(agent=None, **kwargs) -> None:  # noqa: ANN001, ANN003, ARG001
        called["agent"] = agent
        called["passthrough"] = list(kwargs.get("passthrough_args") or [])

    monkeypatch.setattr(run_cmd, "run", _fake_run)

    result = runner.invoke(app, ["j"])
    assert result.exit_code == 0
    assert called["agent"] == "jcode"
    assert called["passthrough"] == []


def test_freebuff_shortcut_runs_freebuff(monkeypatch) -> None:
    called: dict[str, object] = {"agent": None, "passthrough": None}

    def _fake_run(agent=None, **kwargs) -> None:  # noqa: ANN001, ANN003, ARG001
        called["agent"] = agent
        called["passthrough"] = list(kwargs.get("passthrough_args") or [])

    monkeypatch.setattr(run_cmd, "run", _fake_run)

    result = runner.invoke(app, ["fb"])
    assert result.exit_code == 0
    assert called["agent"] == "freebuff"
    assert called["passthrough"] == []


def test_qwen_shortcut_runs_qwen(monkeypatch) -> None:
    called: dict[str, object] = {"agent": None, "passthrough": None}

    def _fake_run(agent=None, **kwargs) -> None:  # noqa: ANN001, ANN003, ARG001
        called["agent"] = agent
        called["passthrough"] = list(kwargs.get("passthrough_args") or [])

    monkeypatch.setattr(run_cmd, "run", _fake_run)

    result = runner.invoke(app, ["q"])
    assert result.exit_code == 0
    assert called["agent"] == "qwen"
    assert called["passthrough"] == []


def test_copilot_shortcut_still_runs_copilot(monkeypatch) -> None:
    called: dict[str, object] = {"agent": None, "passthrough": None}

    def _fake_run(agent=None, **kwargs) -> None:  # noqa: ANN001, ANN003, ARG001
        called["agent"] = agent
        called["passthrough"] = list(kwargs.get("passthrough_args") or [])

    monkeypatch.setattr(run_cmd, "run", _fake_run)

    result = runner.invoke(app, ["p"])
    assert result.exit_code == 0
    assert called["agent"] == "copilot"
    assert called["passthrough"] == []


def test_alias_forwards_extra_args(monkeypatch) -> None:
    called: dict[str, object] = {"agent": None, "passthrough": None}

    def _fake_run(agent=None, **kwargs) -> None:  # noqa: ANN001, ANN003, ARG001
        called["agent"] = agent
        called["passthrough"] = list(kwargs.get("passthrough_args") or [])

    monkeypatch.setattr(run_cmd, "run", _fake_run)

    result = runner.invoke(app, ["claude", "setup-token"])
    assert result.exit_code == 0
    assert called["agent"] == "claude"
    assert called["passthrough"] == ["setup-token"]


def test_alias_forwards_extra_option_args_after_delimiter(monkeypatch) -> None:
    called: dict[str, object] = {"agent": None, "passthrough": None}

    def _fake_run(agent=None, **kwargs) -> None:  # noqa: ANN001, ANN003, ARG001
        called["agent"] = agent
        called["passthrough"] = list(kwargs.get("passthrough_args") or [])

    monkeypatch.setattr(run_cmd, "run", _fake_run)

    result = runner.invoke(app, ["claude", "--", "--model", "sonnet", "hello"])
    assert result.exit_code == 0
    assert called["agent"] == "claude"
    assert called["passthrough"] == ["--model", "sonnet", "hello"]


def test_run_command_forwards_acp_flag(monkeypatch) -> None:
    called: dict[str, object] = {}

    def _fake_run(agent=None, **kwargs) -> None:  # noqa: ANN001, ANN003, ARG001
        called["agent"] = agent
        called["acp"] = kwargs.get("acp")

    monkeypatch.setattr(run_cmd, "run", _fake_run)

    result = runner.invoke(app, ["run", "claude", "--acp"])
    assert result.exit_code == 0
    assert called["acp"] is True

    result = runner.invoke(app, ["claude", "--acp"])
    assert result.exit_code == 0
    assert called["agent"] == "claude"
    assert called["acp"] is True


def test_run_command_forwards_overlay_flags(monkeypatch) -> None:
    called: dict[str, object] = {}

    def _fake_run(agent=None, **kwargs) -> None:  # noqa: ANN001, ANN003, ARG001
        called["agent"] = agent
        called["no_overlay"] = kwargs.get("no_overlay")
        called["rebuild_overlay"] = kwargs.get("rebuild_overlay")
        called["passthrough"] = list(kwargs.get("passthrough_args") or [])

    monkeypatch.setattr(run_cmd, "run", _fake_run)

    result = runner.invoke(app, ["run", "claude", "--no-overlay", "--rebuild-overlay"])
    assert result.exit_code == 0
    assert called["no_overlay"] is True
    assert called["rebuild_overlay"] is True
    assert called["passthrough"] == []


def test_run_and_alias_forward_publish_flag(monkeypatch) -> None:
    called: dict[str, object] = {}

    def _fake_run(agent=None, **kwargs) -> None:  # noqa: ANN001, ANN003, ARG001
        called["agent"] = agent
        called["publish"] = kwargs.get("publish")
        called["passthrough"] = list(kwargs.get("passthrough_args") or [])

    monkeypatch.setattr(run_cmd, "run", _fake_run)

    result = runner.invoke(app, ["run", "claude", "-p", "127.0.0.1:3090:3081"])
    assert result.exit_code == 0
    assert called["publish"] == ["127.0.0.1:3090:3081"]
    assert called["passthrough"] == []

    result = runner.invoke(app, ["claude", "-p", "8000:8000", "--publish", "6000:6000/udp"])
    assert result.exit_code == 0
    assert called["agent"] == "claude"
    assert called["publish"] == ["8000:8000", "6000:6000/udp"]
    assert called["passthrough"] == []


def test_alias_forwards_overlay_flags(monkeypatch) -> None:
    called: dict[str, object] = {}

    def _fake_run(agent=None, **kwargs) -> None:  # noqa: ANN001, ANN003, ARG001
        called["agent"] = agent
        called["no_overlay"] = kwargs.get("no_overlay")
        called["rebuild_overlay"] = kwargs.get("rebuild_overlay")
        called["passthrough"] = list(kwargs.get("passthrough_args") or [])

    monkeypatch.setattr(run_cmd, "run", _fake_run)

    result = runner.invoke(app, ["claude", "--no-overlay", "--rebuild-overlay"])
    assert result.exit_code == 0
    assert called["agent"] == "claude"
    assert called["no_overlay"] is True
    assert called["rebuild_overlay"] is True
    assert called["passthrough"] == []
