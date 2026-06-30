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


def test_python314_http_response_flush_filter_matches_closed_fp_error() -> None:
    response = SimpleNamespace(fp=SimpleNamespace(closed=True))
    exc = ValueError("I/O operation on closed file.")

    assert should_ignore_closed_http_response_flush_error(response, exc) is True


def test_python314_http_response_flush_filter_does_not_hide_other_errors() -> None:
    closed_response = SimpleNamespace(fp=SimpleNamespace(closed=True))
    open_response = SimpleNamespace(fp=SimpleNamespace(closed=False))

    assert (
        should_ignore_closed_http_response_flush_error(
            closed_response, ValueError("different error")
        )
        is False
    )
    assert (
        should_ignore_closed_http_response_flush_error(
            open_response, ValueError("I/O operation on closed file.")
        )
        is False
    )
    assert (
        should_ignore_closed_http_response_flush_error(
            closed_response, RuntimeError("I/O operation on closed file.")
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
