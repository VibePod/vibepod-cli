"""Configuration tests."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from vibepod.cli import app
from vibepod.core.config import deep_merge

runner = CliRunner()


def test_deep_merge() -> None:
    base = {"a": 1, "nested": {"x": 1, "y": 2}}
    override = {"nested": {"y": 999, "z": 3}, "b": 2}
    merged = deep_merge(base, override)
    assert merged == {"a": 1, "b": 2, "nested": {"x": 1, "y": 999, "z": 3}}


def test_config_init_creates_project_config() -> None:
    with runner.isolated_filesystem():
        result = runner.invoke(app, ["config", "init"])
        assert result.exit_code == 0
        project_config = Path(".vibepod/config.yaml")
        assert project_config.exists()
        assert project_config.read_text(encoding="utf-8") == "version: 1\n"


def test_config_init_does_not_overwrite_existing_config_without_force() -> None:
    with runner.isolated_filesystem():
        project_config = Path(".vibepod/config.yaml")
        project_config.parent.mkdir(parents=True, exist_ok=True)
        project_config.write_text("default_agent: codex\n", encoding="utf-8")

        result = runner.invoke(app, ["config", "init"])
        assert result.exit_code == 1
        assert "already exists" in result.stdout
        assert project_config.read_text(encoding="utf-8") == "default_agent: codex\n"


def test_config_init_force_overwrites_existing_config() -> None:
    with runner.isolated_filesystem():
        project_config = Path(".vibepod/config.yaml")
        project_config.parent.mkdir(parents=True, exist_ok=True)
        project_config.write_text("default_agent: codex\n", encoding="utf-8")

        result = runner.invoke(app, ["config", "init", "--force"])
        assert result.exit_code == 0
        assert project_config.read_text(encoding="utf-8") == "version: 1\n"
