"""Configuration tests."""

from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from vibepod.cli import app
from vibepod.constants import SUPPORTED_AGENTS
from vibepod.core.config import deep_merge, get_config

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


def test_config_init_with_agent_creates_project_config_with_agent_block() -> None:
    with runner.isolated_filesystem():
        result = runner.invoke(app, ["config", "init", "claude"])
        assert result.exit_code == 0

        project_config = Path(".vibepod/config.yaml")
        loaded = yaml.safe_load(project_config.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        assert loaded["version"] == 1
        assert isinstance(loaded.get("agents"), dict)
        assert isinstance(loaded["agents"].get("claude"), dict)
        assert loaded["agents"]["claude"]["enabled"] is True
        assert loaded["agents"]["claude"]["env"] == {}
        assert loaded["agents"]["claude"]["volumes"] == []
        assert loaded["agents"]["claude"]["init"] == []
        assert isinstance(loaded["agents"]["claude"]["image"], str)


def test_config_init_with_agent_appends_to_existing_project_config() -> None:
    with runner.isolated_filesystem():
        project_config = Path(".vibepod/config.yaml")
        project_config.parent.mkdir(parents=True, exist_ok=True)
        project_config.write_text("version: 1\ndefault_agent: codex\n", encoding="utf-8")

        result = runner.invoke(app, ["config", "init", "gemini"])
        assert result.exit_code == 0

        loaded = yaml.safe_load(project_config.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        assert loaded["default_agent"] == "codex"
        assert isinstance(loaded.get("agents"), dict)
        assert isinstance(loaded["agents"].get("gemini"), dict)


def test_config_init_with_agent_fails_when_agent_already_configured() -> None:
    with runner.isolated_filesystem():
        project_config = Path(".vibepod/config.yaml")
        project_config.parent.mkdir(parents=True, exist_ok=True)
        project_config.write_text(
            "version: 1\nagents:\n  claude:\n    enabled: true\n",
            encoding="utf-8",
        )

        result = runner.invoke(app, ["config", "init", "claude"])
        assert result.exit_code == 1
        assert "already contains agent 'claude'" in result.stdout

        loaded = yaml.safe_load(project_config.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        assert isinstance(loaded.get("agents"), dict)
        assert loaded["agents"]["claude"]["enabled"] is True


def test_default_config_exposes_agent_init(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path))
    config = get_config()
    agents = config.get("agents", {})
    assert isinstance(agents, dict)

    for agent in SUPPORTED_AGENTS:
        assert agents[agent]["init"] == []


def test_default_config_exposes_agent_auto_pull(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path))
    config = get_config()
    agents = config.get("agents", {})
    assert isinstance(agents, dict)

    for agent in SUPPORTED_AGENTS:
        assert agents[agent]["auto_pull"] is None


def test_default_config_exposes_container_userns_mode(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path))
    config = get_config()
    assert config["container_userns_mode"] is None


def test_container_userns_mode_env_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("VP_CONTAINER_USERNS_MODE", "keep-id")
    config = get_config()
    assert config["container_userns_mode"] == "keep-id"


def test_per_agent_auto_pull_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path))
    global_config = tmp_path / "config.yaml"
    global_config.write_text(
        "auto_pull: false\nagents:\n  claude:\n    auto_pull: true\n",
        encoding="utf-8",
    )
    config = get_config()
    assert config["auto_pull"] is False
    assert config["agents"]["claude"]["auto_pull"] is True
    # Other agents should still have None (unset)
    assert config["agents"]["gemini"]["auto_pull"] is None
