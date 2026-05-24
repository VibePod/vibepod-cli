"""Driver-level tests: ensure run_engine emits the right docker invocation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from vibepod.core import skills_engine


def _fake_run_factory(stdout: str = "", exit_code: int = 0) -> Any:
    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=exit_code, stdout=stdout, stderr="")

    return fake_run, captured


def test_run_engine_builds_expected_docker_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(skills_engine, "USER_SKILLS_DIR", tmp_path / "user")
    monkeypatch.setattr(skills_engine, "SKILLS_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(skills_engine, "SKILLS_ENGINE_IMAGE", "vibepod/skills-engine:test")
    monkeypatch.chdir(tmp_path)

    fake_run, captured = _fake_run_factory(stdout=json.dumps([{"command": "list", "skills": []}]))
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = skills_engine.list_skills()

    assert result.exit_code == 0
    cmd = captured["cmd"]
    assert cmd[0:3] == ["docker", "run", "--rm"]
    assert "vibepod/skills-engine:test" in cmd
    assert "--json" in cmd
    assert "list" in cmd
    # all three mount sources are present
    mount_args = [arg for i, arg in enumerate(cmd) if cmd[i - 1] == "-v"]
    empty_local = tmp_path / "cache" / "empty-local-skills"
    assert f"{empty_local}:/vibepod/local-skills" in mount_args
    assert any("/vibepod/user-skills" in m for m in mount_args)
    assert any("/vibepod/cache" in m for m in mount_args)
    assert not (tmp_path / ".vibepod").exists()


def test_run_engine_explicit_local_scope_creates_local_skills_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(skills_engine, "USER_SKILLS_DIR", tmp_path / "user")
    monkeypatch.setattr(skills_engine, "SKILLS_CACHE_DIR", tmp_path / "cache")

    fake_run, captured = _fake_run_factory(stdout=json.dumps([]))
    monkeypatch.setattr(subprocess, "run", fake_run)

    skills_engine.list_skills("local", cwd=tmp_path)

    local = tmp_path / ".vibepod" / "skills"
    mount_args = [
        arg for i, arg in enumerate(captured["cmd"]) if captured["cmd"][i - 1] == "-v"
    ]
    assert local.is_dir()
    assert f"{local}:/vibepod/local-skills" in mount_args


def test_run_engine_propagates_trusted_sources_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(skills_engine, "USER_SKILLS_DIR", tmp_path / "user")
    monkeypatch.setattr(skills_engine, "SKILLS_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("VIBEPOD_TRUSTED_SOURCES", "github:vibepod/")
    monkeypatch.chdir(tmp_path)

    fake_run, captured = _fake_run_factory(stdout=json.dumps([]))
    monkeypatch.setattr(subprocess, "run", fake_run)

    skills_engine.list_skills()
    flat = " ".join(captured["cmd"])
    assert "VIBEPOD_TRUSTED_SOURCES=github:vibepod/" in flat


def test_run_engine_raises_on_non_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(skills_engine, "USER_SKILLS_DIR", tmp_path / "user")
    monkeypatch.setattr(skills_engine, "SKILLS_CACHE_DIR", tmp_path / "cache")
    monkeypatch.chdir(tmp_path)

    fake_run, _ = _fake_run_factory(stdout="not json at all")
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(skills_engine.SkillsEngineError):
        skills_engine.list_skills()


def test_add_mounts_local_locator_from_cwd_read_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cwd = tmp_path / "project"
    process_cwd = tmp_path / "process-cwd"
    source = cwd / "skills" / "researcher"
    source.mkdir(parents=True)
    process_cwd.mkdir()
    monkeypatch.setattr(skills_engine, "USER_SKILLS_DIR", tmp_path / "user")
    monkeypatch.setattr(skills_engine, "SKILLS_CACHE_DIR", tmp_path / "cache")
    monkeypatch.chdir(process_cwd)

    fake_run, captured = _fake_run_factory(stdout=json.dumps([]))
    monkeypatch.setattr(subprocess, "run", fake_run)

    skills_engine.add("./skills/researcher", scope="local", cwd=cwd)

    cmd = captured["cmd"]
    mount_args = [arg for i, arg in enumerate(cmd) if cmd[i - 1] == "-v"]
    assert f"{source.resolve()}:{source.resolve()}:ro" in mount_args
    assert "add" in cmd
    assert "-w" in cmd
    assert str(cwd.resolve()) in cmd
    assert "./skills/researcher" in cmd
    assert "/vibepod/source-in" not in cmd


def test_add_accepts_github_tree_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(skills_engine, "USER_SKILLS_DIR", tmp_path / "user")
    monkeypatch.setattr(skills_engine, "SKILLS_CACHE_DIR", tmp_path / "cache")

    fake_run, captured = _fake_run_factory(stdout=json.dumps([]))
    monkeypatch.setattr(subprocess, "run", fake_run)

    url = (
        "https://github.com/alirezarezvani/claude-skills/tree/main/"
        "product-team/skills/spec-to-repo"
    )
    skills_engine.add(url, scope="user", cwd=tmp_path)

    cmd = captured["cmd"]
    expected = "github:alirezarezvani/claude-skills//product-team/skills/spec-to-repo#main"
    assert expected in cmd
    assert url not in cmd


def test_add_rejects_missing_local_locator(tmp_path: Path) -> None:
    with pytest.raises(skills_engine.SkillsEngineError, match="Local skill locator not found"):
        skills_engine.add("./missing", scope="user", cwd=tmp_path)
