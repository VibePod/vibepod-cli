"""Profile core tests: paths, validation, listing, resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from vibepod.core.agents import agent_config_dir
from vibepod.core.profiles import (
    DEFAULT_PROFILE,
    create_profile,
    list_profiles,
    profile_exists,
    remove_profile,
    resolve_profile,
    validate_profile_name,
)


@pytest.fixture()
def config_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("VP_PROFILE", raising=False)
    return tmp_path


def test_agent_config_dir_default_profile_keeps_legacy_path(config_root: Path) -> None:
    assert agent_config_dir("claude") == config_root / "agents" / "claude"
    assert agent_config_dir("claude", DEFAULT_PROFILE) == config_root / "agents" / "claude"


def test_agent_config_dir_named_profile(config_root: Path) -> None:
    assert (
        agent_config_dir("claude", "work")
        == config_root / "profiles" / "work" / "agents" / "claude"
    )
    assert (
        agent_config_dir("gemini", "ollama")
        == config_root / "profiles" / "ollama" / "agents" / "gemini"
    )


def test_validate_profile_name_accepts_slugs() -> None:
    for name in ("work", "api-key", "ollama_local", "a", "x1"):
        validate_profile_name(name)


def test_validate_profile_name_rejects_bad_names() -> None:
    for name in ("", "Work", "-lead", "_lead", "sp ace", "dot.name", "a/b", "..", "über"):
        with pytest.raises(ValueError):
            validate_profile_name(name)


def test_default_profile_always_exists(config_root: Path) -> None:
    assert profile_exists(DEFAULT_PROFILE) is True
    assert list_profiles() == [DEFAULT_PROFILE]


def test_create_and_list_profiles(config_root: Path) -> None:
    create_profile("work")
    create_profile("apikey")
    assert list_profiles() == [DEFAULT_PROFILE, "apikey", "work"]
    assert profile_exists("work") is True
    assert profile_exists("missing") is False
    assert (config_root / "profiles" / "work" / "agents").is_dir()


def test_create_profile_rejects_default_and_duplicates(config_root: Path) -> None:
    with pytest.raises(ValueError):
        create_profile(DEFAULT_PROFILE)
    create_profile("work")
    with pytest.raises(ValueError):
        create_profile("work")


def test_remove_profile(config_root: Path) -> None:
    create_profile("work")
    remove_profile("work")
    assert profile_exists("work") is False
    with pytest.raises(ValueError):
        remove_profile(DEFAULT_PROFILE)
    with pytest.raises(ValueError):
        remove_profile("missing")


def test_resolve_profile_precedence(
    config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_profile("flagged")
    create_profile("envvar")
    create_profile("configured")

    # config key < env var < CLI flag
    config = {"profile": "configured"}
    assert resolve_profile(None, {}) == DEFAULT_PROFILE
    assert resolve_profile(None, config) == "configured"
    monkeypatch.setenv("VP_PROFILE", "envvar")
    assert resolve_profile(None, config) == "envvar"
    assert resolve_profile("flagged", config) == "flagged"


def test_resolve_profile_unknown_raises(config_root: Path) -> None:
    with pytest.raises(ValueError, match="vp profile create"):
        resolve_profile("nope", {})


def test_resolve_profile_rejects_traversal_names(
    config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("..", "../../tmp", "a/b"):
        with pytest.raises(ValueError, match="Invalid profile name"):
            resolve_profile(name, {})
    monkeypatch.setenv("VP_PROFILE", "..")
    with pytest.raises(ValueError, match="Invalid profile name"):
        resolve_profile(None, {})


def test_remove_profile_rejects_traversal_names(config_root: Path) -> None:
    (config_root / "profiles").mkdir()
    for name in ("..", "../..", "a/b"):
        with pytest.raises(ValueError, match="Invalid profile name"):
            remove_profile(name)
    assert config_root.is_dir()
