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
    called: dict[str, object] = {"agent": None, "passthrough": None}

    def _fake_run(agent=None, **kwargs) -> None:  # noqa: ANN001, ANN003, ARG001
        import click

        ctx = click.get_current_context(silent=True)
        called["agent"] = agent
        called["passthrough"] = list(ctx.args) if ctx and ctx.args else []

    monkeypatch.setattr(run_cmd, "run", _fake_run)

    result = runner.invoke(app, ["claude"])
    assert result.exit_code == 0
    assert called["agent"] == "claude"
    assert called["passthrough"] == []


def test_alias_forwards_extra_args(monkeypatch) -> None:
    called: dict[str, object] = {"agent": None, "passthrough": None}

    def _fake_run(agent=None, **kwargs) -> None:  # noqa: ANN001, ANN003, ARG001
        import click

        ctx = click.get_current_context(silent=True)
        called["agent"] = agent
        called["passthrough"] = list(ctx.args) if ctx and ctx.args else []

    monkeypatch.setattr(run_cmd, "run", _fake_run)

    result = runner.invoke(app, ["claude", "setup-token"])
    assert result.exit_code == 0
    assert called["agent"] == "claude"
    assert called["passthrough"] == ["setup-token"]
