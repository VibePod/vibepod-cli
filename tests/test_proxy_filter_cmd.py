"""Tests for vp proxy filter commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from vibepod.cli import app

runner = CliRunner()


@pytest.fixture()
def config_dir(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("VP_PROXY_ENABLED", "true")
    monkeypatch.delenv("VP_PROXY_FILTER_MODE", raising=False)
    monkeypatch.chdir(tmp_path)
    # Point the proxy db under the temp dir so filter.json lands there too.
    (tmp_path / "config.yaml").write_text(
        f"proxy:\n  db_path: {tmp_path / 'proxy' / 'proxy.db'}\n",
    )
    return tmp_path


def _filter_json(config_dir: Path) -> dict:
    return json.loads((config_dir / "proxy" / "filter.json").read_text())


def _config_yaml(config_dir: Path) -> dict:
    return yaml.safe_load((config_dir / "config.yaml").read_text())


def test_filter_status_shows_defaults(config_dir: Path) -> None:
    result = runner.invoke(app, ["proxy", "filter", "status"])
    assert result.exit_code == 0
    assert "open" in result.stdout


def test_filter_mode_switch_updates_config_and_file(config_dir: Path) -> None:
    result = runner.invoke(app, ["proxy", "filter", "mode", "allow"])
    assert result.exit_code == 0
    assert _config_yaml(config_dir)["proxy"]["filter"]["mode"] == "allow"
    assert _filter_json(config_dir)["mode"] == "allow"


def test_filter_mode_rejects_unknown(config_dir: Path) -> None:
    result = runner.invoke(app, ["proxy", "filter", "mode", "strict"])
    assert result.exit_code == 1


def test_allow_add_and_remove(config_dir: Path) -> None:
    result = runner.invoke(app, ["proxy", "filter", "allow", "add", "API.Anthropic.com"])
    assert result.exit_code == 0
    assert _config_yaml(config_dir)["proxy"]["filter"]["allow"] == ["api.anthropic.com"]
    assert _filter_json(config_dir)["allow"] == ["api.anthropic.com"]

    result = runner.invoke(app, ["proxy", "filter", "allow", "remove", "api.anthropic.com"])
    assert result.exit_code == 0
    assert _config_yaml(config_dir)["proxy"]["filter"]["allow"] == []
    assert _filter_json(config_dir)["allow"] == []


def test_deny_add_wildcard(config_dir: Path) -> None:
    result = runner.invoke(app, ["proxy", "filter", "deny", "add", "*.example.com"])
    assert result.exit_code == 0
    assert _filter_json(config_dir)["deny"] == ["*.example.com"]


def test_add_rejects_invalid_pattern(config_dir: Path) -> None:
    result = runner.invoke(app, ["proxy", "filter", "allow", "add", "https://example.com"])
    assert result.exit_code == 1


def test_add_duplicate_is_noop(config_dir: Path) -> None:
    runner.invoke(app, ["proxy", "filter", "allow", "add", "a.com"])
    result = runner.invoke(app, ["proxy", "filter", "allow", "add", "a.com"])
    assert result.exit_code == 0
    assert _config_yaml(config_dir)["proxy"]["filter"]["allow"] == ["a.com"]


def test_remove_absent_is_noop(config_dir: Path) -> None:
    result = runner.invoke(app, ["proxy", "filter", "allow", "remove", "missing.com"])
    assert result.exit_code == 0
    assert _config_yaml(config_dir)["proxy"]["filter"].get("allow", []) == []


def test_mode_switch_preserves_lists(config_dir: Path) -> None:
    runner.invoke(app, ["proxy", "filter", "deny", "add", "example.com"])
    runner.invoke(app, ["proxy", "filter", "mode", "allow"])
    data = _config_yaml(config_dir)["proxy"]["filter"]
    assert data["mode"] == "allow"
    assert data["deny"] == ["example.com"]


def test_remove_matches_hand_authored_unnormalized_entry(config_dir: Path) -> None:
    (config_dir / "config.yaml").write_text(
        f"proxy:\n  db_path: {config_dir / 'proxy' / 'proxy.db'}\n"
        "  filter:\n    mode: deny\n    deny:\n      - Example.COM.\n",
    )
    result = runner.invoke(app, ["proxy", "filter", "deny", "remove", "example.com"])
    assert result.exit_code == 0
    assert _config_yaml(config_dir)["proxy"]["filter"]["deny"] == []


def test_add_does_not_duplicate_unnormalized_entry(config_dir: Path) -> None:
    (config_dir / "config.yaml").write_text(
        f"proxy:\n  db_path: {config_dir / 'proxy' / 'proxy.db'}\n"
        "  filter:\n    mode: deny\n    deny:\n      - Example.COM.\n",
    )
    result = runner.invoke(app, ["proxy", "filter", "deny", "add", "example.com"])
    assert result.exit_code == 0
    assert len(_config_yaml(config_dir)["proxy"]["filter"]["deny"]) == 1


def test_mode_warns_when_project_config_overrides(config_dir: Path) -> None:
    project = config_dir / ".vibepod"
    project.mkdir()
    (project / "config.yaml").write_text("proxy:\n  filter:\n    mode: deny\n")

    result = runner.invoke(app, ["proxy", "filter", "mode", "allow"])

    assert result.exit_code == 0
    assert "overridden" in result.stdout
    assert _filter_json(config_dir)["mode"] == "deny"


def test_mode_warns_when_env_overrides(config_dir: Path, monkeypatch) -> None:
    monkeypatch.setenv("VP_PROXY_FILTER_MODE", "deny")

    result = runner.invoke(app, ["proxy", "filter", "mode", "allow"])

    assert result.exit_code == 0
    assert "overridden" in result.stdout
    assert _filter_json(config_dir)["mode"] == "deny"


def test_add_warns_when_project_config_overrides(config_dir: Path) -> None:
    project = config_dir / ".vibepod"
    project.mkdir()
    (project / "config.yaml").write_text("proxy:\n  filter:\n    allow: []\n")

    result = runner.invoke(app, ["proxy", "filter", "allow", "add", "a.com"])

    assert result.exit_code == 0
    assert "overridden" in result.stdout
    assert _filter_json(config_dir)["allow"] == []


def test_status_warns_on_invalid_configured_mode(config_dir: Path) -> None:
    (config_dir / "config.yaml").write_text(
        f"proxy:\n  db_path: {config_dir / 'proxy' / 'proxy.db'}\n  filter:\n    mode: strict\n",
    )
    result = runner.invoke(app, ["proxy", "filter", "status"])
    assert result.exit_code == 0
    assert "strict" in result.stdout
    assert "open" in result.stdout
