"""CLI smoke tests."""

from __future__ import annotations

from typer.testing import CliRunner

from vibepod.cli import app

runner = CliRunner()


def test_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "VibePod" in result.stdout


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "VibePod CLI" in result.stdout
