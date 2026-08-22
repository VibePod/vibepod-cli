"""Tests for proxy filter rule management and materialization."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from vibepod.core import proxy_filter as pf
from vibepod.core.config import get_config


def test_normalize_pattern_lowercases_and_strips() -> None:
    assert pf.normalize_pattern("  Example.COM. ") == "example.com"


def test_normalize_pattern_accepts_wildcard() -> None:
    assert pf.normalize_pattern("*.GitHub.com") == "*.github.com"


@pytest.mark.parametrize(
    "raw",
    ["", "https://example.com", "example.com/path", "*example.com", "a b.com", "*."],
)
def test_normalize_pattern_rejects_garbage(raw: str) -> None:
    with pytest.raises(ValueError):
        pf.normalize_pattern(raw)


def test_get_filter_settings_defaults_open() -> None:
    assert pf.get_filter_settings({}) == {"mode": "open", "allow": [], "deny": []}


def test_get_filter_settings_coerces_invalid_mode() -> None:
    config = {"proxy": {"filter": {"mode": "strict", "allow": ["a.com"], "deny": []}}}
    assert pf.get_filter_settings(config)["mode"] == "open"


def test_get_filter_settings_passes_valid_config() -> None:
    config = {"proxy": {"filter": {"mode": "allow", "allow": ["a.com"], "deny": ["b.com"]}}}
    assert pf.get_filter_settings(config) == {
        "mode": "allow",
        "allow": ["a.com"],
        "deny": ["b.com"],
    }


def test_write_filter_file_materializes_next_to_db(tmp_path: Path) -> None:
    config = {
        "proxy": {
            "db_path": str(tmp_path / "proxy" / "proxy.db"),
            "filter": {"mode": "deny", "allow": [], "deny": ["example.com"]},
        },
    }
    path = pf.write_filter_file(config)
    assert path == tmp_path / "proxy" / "filter.json"
    assert json.loads(path.read_text()) == {
        "mode": "deny",
        "allow": [],
        "deny": ["example.com"],
    }


def test_update_global_filter_writes_config_yaml(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path))
    pf.update_global_filter(lambda f: f.update(mode="allow"))
    data = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert data["proxy"]["filter"]["mode"] == "allow"


def test_update_global_filter_preserves_other_keys(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.yaml").write_text("default_agent: gemini\nproxy:\n  enabled: true\n")
    pf.update_global_filter(lambda f: f.setdefault("allow", []).append("a.com"))
    data = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert data["default_agent"] == "gemini"
    assert data["proxy"]["enabled"] is True
    assert data["proxy"]["filter"]["allow"] == ["a.com"]


def test_default_config_has_open_filter(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    config = get_config()
    assert config["proxy"]["filter"] == {"mode": "open", "allow": [], "deny": []}


def test_env_overrides_filter_mode(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VP_PROXY_FILTER_MODE", "deny")
    config = get_config()
    assert config["proxy"]["filter"]["mode"] == "deny"


def test_write_filter_file_is_atomic(monkeypatch, tmp_path: Path) -> None:
    """No truncate-then-write: replace so hot-reload readers never see partials."""
    config = {
        "proxy": {
            "db_path": str(tmp_path / "proxy" / "proxy.db"),
            "filter": {"mode": "deny", "allow": [], "deny": ["example.com"]},
        },
    }
    pf.write_filter_file(config)

    calls: list[tuple[Path, Path]] = []
    real_replace = pf.os.replace

    def spy_replace(src, dst):
        calls.append((Path(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(pf.os, "replace", spy_replace)
    path = pf.write_filter_file(config)
    assert calls and calls[-1][1] == path
    assert json.loads(path.read_text())["mode"] == "deny"
    assert list(path.parent.glob(f".{path.name}.*")) == []


def test_update_global_filter_refuses_non_mapping_config(monkeypatch, tmp_path: Path) -> None:
    """A list-root config.yaml must not be silently replaced (data loss)."""
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.yaml").write_text("- just\n- a\n- list\n")
    with pytest.raises(ValueError):
        pf.update_global_filter(lambda f: f.update(mode="allow"))
    assert yaml.safe_load((tmp_path / "config.yaml").read_text()) == ["just", "a", "list"]


def test_write_filter_file_warns_on_invalid_mode(tmp_path: Path, capsys) -> None:
    """Every startup path materializes; the fail-open coercion must be visible."""
    config = {
        "proxy": {
            "db_path": str(tmp_path / "proxy" / "proxy.db"),
            "filter": {"mode": "alow", "allow": [], "deny": []},
        },
    }
    path = pf.write_filter_file(config)
    assert json.loads(path.read_text())["mode"] == "open"
    out = capsys.readouterr().out
    assert "alow" in out
    assert "open" in out


def test_atomic_write_preserves_symlinked_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path))
    target = tmp_path / "dotfiles" / "vibepod.yaml"
    target.parent.mkdir()
    target.write_text("default_agent: gemini\n")
    (tmp_path / "config.yaml").symlink_to(target)

    pf.update_global_filter(lambda f: f.update(mode="allow"))

    assert (tmp_path / "config.yaml").is_symlink()
    data = yaml.safe_load(target.read_text())
    assert data["default_agent"] == "gemini"
    assert data["proxy"]["filter"]["mode"] == "allow"
