"""--profile plumbing tests for run/task/doctor."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from vibepod.cli import app
from vibepod.commands import run as run_cmd
from vibepod.core.agents import agent_config_dir
from vibepod.core.docker import DockerClientError
from vibepod.core.profiles import create_profile

runner = CliRunner()


@pytest.fixture()
def config_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("VP_PROFILE", raising=False)
    monkeypatch.setattr(run_cmd, "is_dir_allowed", lambda p: True)
    return tmp_path


def test_run_unknown_profile_fails_fast(config_root: Path) -> None:
    result = runner.invoke(app, ["run", "claude", "--profile", "nope"])
    assert result.exit_code == 1
    assert "vp profile create nope" in result.output


def test_run_unknown_profile_via_vp_profile_env(
    config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VP_PROFILE", "ghost")
    result = runner.invoke(app, ["run", "claude"])
    assert result.exit_code == 1
    assert "vp profile create ghost" in result.output


def test_run_uses_profile_config_dir_for_stored_token(
    config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_profile("work")
    seen: dict[str, Path] = {}

    def fake_read(config_dir: Path) -> str | None:
        seen["config_dir"] = config_dir
        return None

    def broken_docker() -> None:
        raise DockerClientError("docker unavailable in test")

    monkeypatch.setattr(run_cmd, "_read_claude_stored_token", fake_read)
    monkeypatch.setattr(run_cmd, "DockerManager", broken_docker)

    result = runner.invoke(app, ["run", "claude", "--profile", "work"])
    assert result.exit_code != 0  # stops at docker, after token lookup
    assert seen["config_dir"] == agent_config_dir("claude", "work")


def test_task_create_unknown_profile_fails_fast(config_root: Path) -> None:
    result = runner.invoke(app, ["task", "create", "claude", "hi", "--profile", "nope"])
    assert result.exit_code == 1
    assert "vp profile create nope" in result.output


def test_doctor_claude_reports_profile_dir(config_root: Path) -> None:
    create_profile("work")
    profile_dir = agent_config_dir("claude", "work")
    profile_dir.mkdir(parents=True)

    result = runner.invoke(app, ["doctor", "claude", "--profile", "work"])
    assert str(profile_dir) in result.output.replace("\n", "")


def test_doctor_claude_unknown_profile_fails(config_root: Path) -> None:
    result = runner.invoke(app, ["doctor", "claude", "--profile", "nope"])
    assert result.exit_code == 1
    assert "vp profile create nope" in result.output
