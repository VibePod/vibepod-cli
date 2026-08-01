"""Tests for `vp profile` subcommands."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from vibepod.cli import app
from vibepod.core.agents import agent_config_dir

runner = CliRunner()


@pytest.fixture()
def config_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("VP_PROFILE", raising=False)
    return tmp_path


def test_profile_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["profile", "--help"])
    assert result.exit_code == 0
    for sub in ("list", "create", "remove"):
        assert sub in result.stdout


def test_profile_create_and_list(config_root: Path) -> None:
    result = runner.invoke(app, ["profile", "create", "work"])
    assert result.exit_code == 0, result.output
    assert (config_root / "profiles" / "work" / "agents").is_dir()

    result = runner.invoke(app, ["profile", "list"])
    assert result.exit_code == 0
    assert "default" in result.stdout
    assert "work" in result.stdout


def test_profile_list_marks_agents_with_credentials(config_root: Path) -> None:
    runner.invoke(app, ["profile", "create", "work"])
    creds = agent_config_dir("claude", "work")
    creds.mkdir(parents=True)
    (creds / "oauth-token").write_text("tok\n")

    result = runner.invoke(app, ["profile", "list"])
    assert result.exit_code == 0
    assert "claude" in result.stdout


def test_profile_create_rejects_invalid_name(config_root: Path) -> None:
    result = runner.invoke(app, ["profile", "create", "Bad Name"])
    assert result.exit_code != 0
    result = runner.invoke(app, ["profile", "create", "default"])
    assert result.exit_code != 0


def test_profile_create_rejects_duplicate(config_root: Path) -> None:
    assert runner.invoke(app, ["profile", "create", "work"]).exit_code == 0
    result = runner.invoke(app, ["profile", "create", "work"])
    assert result.exit_code != 0


def test_profile_remove(config_root: Path) -> None:
    runner.invoke(app, ["profile", "create", "work"])
    result = runner.invoke(app, ["profile", "remove", "work", "--yes"])
    assert result.exit_code == 0, result.output
    assert not (config_root / "profiles" / "work").exists()


def test_profile_remove_refuses_default(config_root: Path) -> None:
    result = runner.invoke(app, ["profile", "remove", "default", "--yes"])
    assert result.exit_code != 0


def test_profile_remove_unknown(config_root: Path) -> None:
    result = runner.invoke(app, ["profile", "remove", "missing", "--yes"])
    assert result.exit_code != 0


def test_profile_remove_validates_before_confirmation(config_root: Path) -> None:
    # invalid/unknown names must error out without ever prompting
    result = runner.invoke(app, ["profile", "remove", "missing"])
    assert result.exit_code == 1
    assert "does not exist" in result.output
    assert "Remove profile" not in result.output

    result = runner.invoke(app, ["profile", "remove", ".."])
    assert result.exit_code == 1
    assert "Invalid profile name" in result.output
    assert "Remove profile" not in result.output


def test_profile_list_broken_selection_shows_warning_not_default(
    config_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VP_PROFILE", "ghost")
    result = runner.invoke(app, ["profile", "list"])
    assert result.exit_code == 0
    assert "ghost" in result.output
    assert "* default" not in result.output


def test_profile_remove_reports_filesystem_errors(
    config_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner.invoke(app, ["profile", "create", "work"])

    def broken_rmtree(path: object) -> None:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr("vibepod.core.profiles.shutil.rmtree", broken_rmtree)
    result = runner.invoke(app, ["profile", "remove", "work", "--yes"])
    assert result.exit_code == 1
    assert not isinstance(result.exception, OSError)  # handled, no traceback
    assert "work" in result.output
    assert "Permission denied" in result.output


def test_profile_remove_asks_for_confirmation(config_root: Path) -> None:
    runner.invoke(app, ["profile", "create", "work"])
    result = runner.invoke(app, ["profile", "remove", "work"], input="n\n")
    assert result.exit_code != 0
    assert (config_root / "profiles" / "work").exists()
