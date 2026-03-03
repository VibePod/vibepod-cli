"""CLI smoke tests."""

from __future__ import annotations

from typer.testing import CliRunner

from vibepod.cli import app
from vibepod.commands import run as run_cmd

runner = CliRunner()


def test_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "VibePod" in result.stdout


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "VibePod CLI" in result.stdout


def test_full_agent_name_alias_runs_agent(monkeypatch) -> None:
    called: dict[str, str | None] = {"agent": None}

    def _fake_run(agent=None, **kwargs) -> None:  # noqa: ANN001, ANN003, ARG001
        called["agent"] = agent

    monkeypatch.setattr(run_cmd, "run", _fake_run)

    result = runner.invoke(app, ["claude"])
    assert result.exit_code == 0
    assert called["agent"] == "claude"
