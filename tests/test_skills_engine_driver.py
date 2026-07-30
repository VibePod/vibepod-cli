"""Driver-level tests: ensure run_engine emits the right docker invocation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from vibepod.core import skills_engine


@pytest.fixture(autouse=True)
def mock_docker_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDockerManager:
        def __init__(self) -> None:
            self.client = MagicMock()

        def pull_image(self, image: str, auto_clean: bool = False) -> None:
            pass

        def pull_if_newer(self, image: str, auto_clean: bool = False) -> bool:
            return False

    monkeypatch.setattr(skills_engine, "DockerManager", FakeDockerManager)
    monkeypatch.setattr(skills_engine, "_skills_engine_checked", False)
    monkeypatch.setattr(skills_engine, "get_config", lambda: {})


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
    mount_args = [arg for i, arg in enumerate(captured["cmd"]) if captured["cmd"][i - 1] == "-v"]
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
        "https://github.com/alirezarezvani/claude-skills/tree/main/product-team/skills/spec-to-repo"
    )
    skills_engine.add(url, scope="user", cwd=tmp_path)

    cmd = captured["cmd"]
    expected = "github:alirezarezvani/claude-skills//product-team/skills/spec-to-repo#main"
    assert expected in cmd
    assert url not in cmd


def test_add_rejects_missing_local_locator(tmp_path: Path) -> None:
    with pytest.raises(skills_engine.SkillsEngineError, match="Local skill locator not found"):
        skills_engine.add("./missing", scope="user", cwd=tmp_path)


def test_add_mounts_bare_relative_local_locator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cwd = tmp_path / "project"
    source = cwd / "skills" / "researcher"
    source.mkdir(parents=True)
    monkeypatch.setattr(skills_engine, "USER_SKILLS_DIR", tmp_path / "user")
    monkeypatch.setattr(skills_engine, "SKILLS_CACHE_DIR", tmp_path / "cache")

    fake_run, captured = _fake_run_factory(stdout=json.dumps([]))
    monkeypatch.setattr(subprocess, "run", fake_run)

    skills_engine.add("skills/researcher", scope="user", cwd=cwd)

    cmd = captured["cmd"]
    mount_args = [arg for i, arg in enumerate(cmd) if cmd[i - 1] == "-v"]
    assert f"{source.resolve()}:{source.resolve()}:ro" in mount_args
    assert "-w" in cmd
    assert str(cwd.resolve()) in cmd
    # locator string reaches the engine unmodified
    assert "skills/researcher" in cmd


def test_add_mounts_current_directory_locator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "skill"
    source.mkdir()
    monkeypatch.setattr(skills_engine, "USER_SKILLS_DIR", tmp_path / "user")
    monkeypatch.setattr(skills_engine, "SKILLS_CACHE_DIR", tmp_path / "cache")

    fake_run, captured = _fake_run_factory(stdout=json.dumps([]))
    monkeypatch.setattr(subprocess, "run", fake_run)

    skills_engine.add(".", scope="user", cwd=source)

    cmd = captured["cmd"]
    mount_args = [arg for i, arg in enumerate(cmd) if cmd[i - 1] == "-v"]
    assert f"{source.resolve()}:{source.resolve()}:ro" in mount_args
    assert "." in cmd


def test_add_does_not_mount_remote_locators(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(skills_engine, "USER_SKILLS_DIR", tmp_path / "user")
    monkeypatch.setattr(skills_engine, "SKILLS_CACHE_DIR", tmp_path / "cache")

    fake_run, captured = _fake_run_factory(stdout=json.dumps([]))
    monkeypatch.setattr(subprocess, "run", fake_run)

    remotes = (
        "github:org/repo",
        "npm:@acme/pkg",
        "https://git.example.com/x.git",
        "git@git.example.com:org/repo.git",
        "ftp://x/y",
    )
    for locator in remotes:
        skills_engine.add(locator, scope="user", cwd=tmp_path)
        cmd = captured["cmd"]
        mount_args = [arg for i, arg in enumerate(cmd) if cmd[i - 1] == "-v"]
        assert not any(m.endswith(":ro") for m in mount_args), locator
        assert "-w" not in cmd, locator


def test_add_expands_tilde_local_locator(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    source = home / "skills" / "foo"
    source.mkdir(parents=True)
    # POSIX expanduser reads HOME, ntpath.expanduser reads USERPROFILE.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(skills_engine, "USER_SKILLS_DIR", tmp_path / "user")
    monkeypatch.setattr(skills_engine, "SKILLS_CACHE_DIR", tmp_path / "cache")

    fake_run, captured = _fake_run_factory(stdout=json.dumps([]))
    monkeypatch.setattr(subprocess, "run", fake_run)

    skills_engine.add("~/skills/foo", scope="user", cwd=tmp_path)

    cmd = captured["cmd"]
    mount_args = [arg for i, arg in enumerate(cmd) if cmd[i - 1] == "-v"]
    assert f"{source.resolve()}:{source.resolve()}:ro" in mount_args
    # the engine receives the expanded absolute path, never a bare "~"
    assert str(home / "skills" / "foo") in cmd
    assert not any(arg.startswith("~") for arg in cmd)


def test_is_local_locator_classifies_paths_and_schemes() -> None:
    """Platform-independent: a Windows drive letter is a path, not a scheme."""
    for local in (
        "skills/foo",
        "./skills/foo",
        "../shared/skills/foo",
        "/abs/path",
        ".",
        "..",
        "~/skills/foo",
        r"C:\dev\skills\foo",
        "C:/dev/skills/foo",
        # a directory literally named "git@..." has no scp remote separator
        "git@local-skill",
        "./git@local-skill",
    ):
        assert skills_engine._is_local_locator(local), local

    for remote in (
        "github:org/repo",
        "gitlab:group/repo",
        "npm:@acme/pkg",
        "https://git.example.com/x.git",
        "http://git.example.com/x.git",
        "ftp://x/y",
        "git@git.example.com:org/repo.git",
    ):
        assert not skills_engine._is_local_locator(remote), remote


def test_add_rejects_missing_bare_local_locator(tmp_path: Path) -> None:
    with pytest.raises(skills_engine.SkillsEngineError, match="Local skill locator not found"):
        skills_engine.add("skills/missing", scope="user", cwd=tmp_path)


def test_run_engine_pulls_image_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(skills_engine, "USER_SKILLS_DIR", tmp_path / "user")
    monkeypatch.setattr(skills_engine, "SKILLS_CACHE_DIR", tmp_path / "cache")
    monkeypatch.chdir(tmp_path)

    pulled_images = []
    checked_images = []

    from vibepod.core.docker import NotFound

    class FakeDockerManager:
        def __init__(self) -> None:
            self.client = MagicMock()
            self.client.images.get.side_effect = NotFound("not found")

        def pull_image(self, image: str, auto_clean: bool = False) -> None:
            pulled_images.append((image, auto_clean))

        def pull_if_newer(self, image: str, auto_clean: bool = False) -> bool:
            checked_images.append(image)
            return False

    monkeypatch.setattr(skills_engine, "DockerManager", FakeDockerManager)
    monkeypatch.setattr(skills_engine, "_skills_engine_checked", False)

    fake_run, _ = _fake_run_factory(stdout=json.dumps([]))
    monkeypatch.setattr(subprocess, "run", fake_run)

    skills_engine.list_skills()

    assert (skills_engine.SKILLS_ENGINE_IMAGE, True) in pulled_images
    assert not checked_images


def test_run_engine_checks_updates_when_latest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(skills_engine, "USER_SKILLS_DIR", tmp_path / "user")
    monkeypatch.setattr(skills_engine, "SKILLS_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(skills_engine, "SKILLS_ENGINE_IMAGE", "vibepod/skills-engine:latest")
    monkeypatch.chdir(tmp_path)

    pulled_images = []
    checked_images = []

    class FakeDockerManager:
        def __init__(self) -> None:
            self.client = MagicMock()
            self.client.images.get.return_value = MagicMock()

        def pull_image(self, image: str, auto_clean: bool = False) -> None:
            pulled_images.append((image, auto_clean))

        def pull_if_newer(self, image: str, auto_clean: bool = False) -> bool:
            checked_images.append((image, auto_clean))
            return False

    monkeypatch.setattr(skills_engine, "DockerManager", FakeDockerManager)
    monkeypatch.setattr(skills_engine, "_skills_engine_checked", False)
    monkeypatch.setattr(
        skills_engine, "get_config", lambda: {"auto_pull": True, "auto_clean": True}
    )

    fake_run, _ = _fake_run_factory(stdout=json.dumps([]))
    monkeypatch.setattr(subprocess, "run", fake_run)

    skills_engine.list_skills()

    assert not pulled_images
    assert ("vibepod/skills-engine:latest", True) in checked_images
