"""Tests for `vp import`."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from vibepod.cli import app

runner = CliRunner()


@pytest.fixture()
def config_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "vibepod"
    monkeypatch.setenv("VP_CONFIG_DIR", str(root))
    monkeypatch.delenv("VP_PROFILE", raising=False)
    return root


@pytest.fixture()
def host_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".claude" / "commands").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text('{"model": "opus"}')
    (home / ".claude" / "commands" / "ship.md").write_text("ship it")
    (home / ".claude" / ".credentials.json").write_text("{}")
    return home


def test_import_copies_into_the_default_profile(config_root: Path, host_home: Path) -> None:
    result = runner.invoke(app, ["import", "claude", "--home", str(host_home)])

    assert result.exit_code == 0, result.output
    agent_dir = config_root / "agents" / "claude"
    assert (agent_dir / "settings.json").read_text() == '{"model": "opus"}'
    assert (agent_dir / "commands" / "ship.md").exists()
    assert not (agent_dir / ".credentials.json").exists()
    assert "--with-credentials" in result.output


def test_dry_run_writes_nothing(config_root: Path, host_home: Path) -> None:
    result = runner.invoke(app, ["import", "claude", "--home", str(host_home), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert not (config_root / "agents" / "claude").exists()
    assert "settings.json" in result.output


def test_existing_destination_aborts_until_forced(config_root: Path, host_home: Path) -> None:
    agent_dir = config_root / "agents" / "claude"
    agent_dir.mkdir(parents=True)
    (agent_dir / "settings.json").write_text("old")

    result = runner.invoke(app, ["import", "claude", "--home", str(host_home)])
    assert result.exit_code == 1
    assert "--force" in result.output
    assert (agent_dir / "settings.json").read_text() == "old"

    forced = runner.invoke(app, ["import", "claude", "--home", str(host_home), "--force"])
    assert forced.exit_code == 0, forced.output
    assert (agent_dir / "settings.json").read_text() == '{"model": "opus"}'


def test_missing_destination_profile_names_the_fix(config_root: Path, host_home: Path) -> None:
    result = runner.invoke(
        app, ["import", "claude", "--home", str(host_home), "--to-profile", "work"]
    )
    assert result.exit_code == 1
    assert "vp profile create work" in result.output


def test_create_profile_flag_creates_it(config_root: Path, host_home: Path) -> None:
    result = runner.invoke(
        app,
        ["import", "claude", "--home", str(host_home), "--to-profile", "work", "--create-profile"],
    )
    assert result.exit_code == 0, result.output
    assert (config_root / "profiles" / "work" / "agents" / "claude" / "settings.json").exists()


def test_only_narrows_the_selection(config_root: Path, host_home: Path) -> None:
    result = runner.invoke(app, ["import", "claude", "--home", str(host_home), "--only", "skills"])
    assert result.exit_code == 0, result.output
    agent_dir = config_root / "agents" / "claude"
    assert (agent_dir / "commands" / "ship.md").exists()
    assert not (agent_dir / "settings.json").exists()


def test_unknown_category_is_rejected(config_root: Path, host_home: Path) -> None:
    result = runner.invoke(app, ["import", "claude", "--home", str(host_home), "--only", "bogus"])
    assert result.exit_code == 1
    assert "bogus" in result.output


def test_unknown_agent_lists_supported_agents(config_root: Path) -> None:
    result = runner.invoke(app, ["import", "nope"])
    assert result.exit_code == 1
    assert "claude" in result.output


def test_no_installation_found_names_checked_paths(config_root: Path, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = runner.invoke(app, ["import", "claude", "--home", str(empty)])
    assert result.exit_code == 1
    assert ".claude" in result.output


def test_bare_import_scans_the_host(config_root: Path, host_home: Path) -> None:
    result = runner.invoke(app, ["import", "--home", str(host_home)])
    assert result.exit_code == 0, result.output
    assert "claude" in result.output
    assert "vp import claude" in result.output


def test_profile_to_profile_copy(config_root: Path) -> None:
    source_dir = config_root / "agents" / "claude"
    source_dir.mkdir(parents=True)
    (source_dir / "settings.json").write_text('{"model": "opus"}')
    (source_dir / "commands").mkdir()
    (source_dir / "commands" / "ship.md").write_text("ship it")
    (source_dir / ".credentials.json").write_text("{}")

    result = runner.invoke(
        app,
        [
            "import",
            "claude",
            "--from-profile",
            "default",
            "--to-profile",
            "work",
            "--create-profile",
        ],
    )

    assert result.exit_code == 0, result.output
    dest = config_root / "profiles" / "work" / "agents" / "claude"
    assert (dest / "settings.json").read_text() == '{"model": "opus"}'
    assert (dest / "commands" / "ship.md").exists()
    assert not (dest / ".credentials.json").exists()


def test_profile_copy_onto_itself_is_refused(config_root: Path) -> None:
    source_dir = config_root / "agents" / "claude"
    source_dir.mkdir(parents=True)
    (source_dir / "settings.json").write_text("{}")

    result = runner.invoke(app, ["import", "claude", "--from-profile", "default"])

    assert result.exit_code == 1
    assert "same directory" in result.output


def test_profile_copy_does_not_replace_an_existing_agent_dir(config_root: Path) -> None:
    source_dir = config_root / "agents" / "claude"
    source_dir.mkdir(parents=True)
    (source_dir / "settings.json").write_text("new")
    dest = config_root / "profiles" / "work" / "agents" / "claude"
    dest.mkdir(parents=True)
    (dest / "settings.json").write_text("old")

    result = runner.invoke(
        app, ["import", "claude", "--from-profile", "default", "--to-profile", "work"]
    )

    assert result.exit_code == 1
    assert "--force" in result.output
    assert (dest / "settings.json").read_text() == "old"


def test_profile_copy_reports_unmapped_files(config_root: Path) -> None:
    source_dir = config_root / "agents" / "claude"
    source_dir.mkdir(parents=True)
    (source_dir / "settings.json").write_text("{}")
    (source_dir / "brand-new-thing.json").write_text("{}")

    result = runner.invoke(
        app,
        [
            "import",
            "claude",
            "--from-profile",
            "default",
            "--to-profile",
            "work",
            "--create-profile",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "unrecognized" in result.output
    assert not (
        config_root / "profiles" / "work" / "agents" / "claude" / "brand-new-thing.json"
    ).exists()


def test_agent_help_lists_categories_and_paths(config_root: Path) -> None:
    result = runner.invoke(app, ["import", "claude", "--help-agent"])

    assert result.exit_code == 0, result.output
    assert "settings" in result.output
    assert ".claude/settings.json" in result.output
    assert "skills" in result.output
    assert "credentials" in result.output
    assert "--with-credentials" in result.output
    assert "Keychain" in result.output  # the entry note is shown


def test_generic_help_mentions_per_agent_help(config_root: Path) -> None:
    result = runner.invoke(app, ["import", "--help"])

    assert result.exit_code == 0
    assert "--help-agent" in result.output
