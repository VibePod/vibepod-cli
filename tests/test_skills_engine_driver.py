"""Driver-level tests: ensure run_engine drives the engine container via the SDK."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
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
    monkeypatch.setattr(skills_engine, "_manager", None)
    monkeypatch.setattr(skills_engine, "get_config", lambda: {})


class _FakeContainer:
    def __init__(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        wait_exc: Exception | None = None,
    ) -> None:
        self._stdout = stdout.encode()
        self._stderr = stderr.encode()
        self._exit_code = exit_code
        self._wait_exc = wait_exc
        self.calls: list[str] = []
        self.removed = False

    def _require_started(self, method: str) -> None:
        # A created-but-unstarted container never runs; mirror the SDK's
        # behaviour of not returning an exit status or logs for it.
        if "start" not in self.calls:
            raise AssertionError(f"{method}() called before start()")

    def start(self) -> None:
        self.calls.append("start")

    def wait(self) -> dict[str, int]:
        self._require_started("wait")
        self.calls.append("wait")
        if self._wait_exc is not None:
            raise self._wait_exc
        return {"StatusCode": self._exit_code}

    def logs(self, stdout: bool = True, stderr: bool = True) -> bytes:
        self._require_started("logs")
        self.calls.append("logs")
        out = b""
        if stdout:
            out += self._stdout
        if stderr:
            out += self._stderr
        return out

    def remove(self, force: bool = False) -> None:
        self.calls.append("remove")
        self.removed = True


def _install_fake_engine(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    wait_exc: Exception | None = None,
) -> dict[str, Any]:
    """Patch DockerManager with a fake whose client captures containers.create."""
    captured: dict[str, Any] = {}

    class FakeDockerManager:
        def __init__(self) -> None:
            container = _FakeContainer(stdout, stderr, exit_code, wait_exc)
            captured["container"] = container

            def create(image: str, **kwargs: Any) -> _FakeContainer:
                captured["image"] = image
                captured["kwargs"] = kwargs
                return container

            self.client = MagicMock()
            self.client.containers.create.side_effect = create

        def pull_image(self, image: str, auto_clean: bool = False) -> None:
            pass

        def pull_if_newer(self, image: str, auto_clean: bool = False) -> bool:
            return False

    monkeypatch.setattr(skills_engine, "DockerManager", FakeDockerManager)
    monkeypatch.setattr(skills_engine, "_skills_engine_checked", False)
    monkeypatch.setattr(skills_engine, "_manager", None)
    return captured


def _ro_mounts(captured: dict[str, Any]) -> dict[str, dict[str, str]]:
    volumes: dict[str, dict[str, str]] = captured["kwargs"]["volumes"]
    return {src: spec for src, spec in volumes.items() if spec.get("mode") == "ro"}


def test_run_engine_runs_via_sdk_without_docker_binary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Podman hosts have no docker binary; the driver must go through the SDK."""
    monkeypatch.setattr(skills_engine, "USER_SKILLS_DIR", tmp_path / "user")
    monkeypatch.setattr(skills_engine, "SKILLS_CACHE_DIR", tmp_path / "cache")
    monkeypatch.chdir(tmp_path)

    def no_docker_binary(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("No such file or directory: 'docker'")

    monkeypatch.setattr(subprocess, "run", no_docker_binary)
    captured = _install_fake_engine(monkeypatch, stdout=json.dumps([]))

    result = skills_engine.list_skills()

    assert result.exit_code == 0
    assert captured["image"] == skills_engine.SKILLS_ENGINE_IMAGE
    # the created container is started before it is waited on, and removed last
    container = captured["container"]
    assert container.calls[:2] == ["start", "wait"]
    assert container.calls[-1] == "remove"
    assert container.removed


def test_run_engine_builds_expected_container_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(skills_engine, "USER_SKILLS_DIR", tmp_path / "user")
    monkeypatch.setattr(skills_engine, "SKILLS_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(skills_engine, "SKILLS_ENGINE_IMAGE", "vibepod/skills-engine:test")
    monkeypatch.chdir(tmp_path)

    captured = _install_fake_engine(
        monkeypatch,
        stdout=json.dumps([{"command": "list", "skills": []}]),
    )

    result = skills_engine.list_skills()

    assert result.exit_code == 0
    assert captured["image"] == "vibepod/skills-engine:test"
    command = captured["kwargs"]["command"]
    assert "--json" in command
    assert "list" in command
    # all three mount sources are present
    volumes = captured["kwargs"]["volumes"]
    empty_local = tmp_path / "cache" / "empty-local-skills"
    assert volumes[str(empty_local)]["bind"] == "/vibepod/local-skills"
    binds = {spec["bind"] for spec in volumes.values()}
    assert "/vibepod/user-skills" in binds
    assert "/vibepod/cache" in binds
    assert not (tmp_path / ".vibepod").exists()


def test_run_engine_explicit_local_scope_creates_local_skills_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(skills_engine, "USER_SKILLS_DIR", tmp_path / "user")
    monkeypatch.setattr(skills_engine, "SKILLS_CACHE_DIR", tmp_path / "cache")

    captured = _install_fake_engine(monkeypatch, stdout=json.dumps([]))

    skills_engine.list_skills("local", cwd=tmp_path)

    local = tmp_path / ".vibepod" / "skills"
    volumes = captured["kwargs"]["volumes"]
    assert local.is_dir()
    assert volumes[str(local)]["bind"] == "/vibepod/local-skills"


def test_run_engine_propagates_trusted_sources_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(skills_engine, "USER_SKILLS_DIR", tmp_path / "user")
    monkeypatch.setattr(skills_engine, "SKILLS_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("VIBEPOD_TRUSTED_SOURCES", "github:vibepod/")
    monkeypatch.chdir(tmp_path)

    captured = _install_fake_engine(monkeypatch, stdout=json.dumps([]))

    skills_engine.list_skills()

    assert captured["kwargs"]["environment"] == {"VIBEPOD_TRUSTED_SOURCES": "github:vibepod/"}


def test_run_engine_raises_on_non_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(skills_engine, "USER_SKILLS_DIR", tmp_path / "user")
    monkeypatch.setattr(skills_engine, "SKILLS_CACHE_DIR", tmp_path / "cache")
    monkeypatch.chdir(tmp_path)

    _install_fake_engine(monkeypatch, stdout="not json at all")

    with pytest.raises(skills_engine.SkillsEngineError):
        skills_engine.list_skills()


def test_run_engine_removes_container_on_wait_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(skills_engine, "USER_SKILLS_DIR", tmp_path / "user")
    monkeypatch.setattr(skills_engine, "SKILLS_CACHE_DIR", tmp_path / "cache")
    monkeypatch.chdir(tmp_path)

    captured = _install_fake_engine(
        monkeypatch,
        stdout=json.dumps([]),
        wait_exc=RuntimeError("socket closed"),
    )

    with pytest.raises(skills_engine.SkillsEngineError, match="socket closed"):
        skills_engine.list_skills()

    assert captured["container"].removed


def test_add_mounts_local_locator_from_cwd_read_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "project"
    process_cwd = tmp_path / "process-cwd"
    source = cwd / "skills" / "researcher"
    source.mkdir(parents=True)
    process_cwd.mkdir()
    monkeypatch.setattr(skills_engine, "USER_SKILLS_DIR", tmp_path / "user")
    monkeypatch.setattr(skills_engine, "SKILLS_CACHE_DIR", tmp_path / "cache")
    monkeypatch.chdir(process_cwd)

    captured = _install_fake_engine(monkeypatch, stdout=json.dumps([]))

    skills_engine.add("./skills/researcher", scope="local", cwd=cwd)

    volumes = captured["kwargs"]["volumes"]
    assert volumes[str(source.resolve())] == {"bind": str(source.resolve()), "mode": "ro"}
    command = captured["kwargs"]["command"]
    assert "add" in command
    assert captured["kwargs"]["working_dir"] == str(cwd.resolve())
    assert "./skills/researcher" in command
    assert "/vibepod/source-in" not in command


def test_add_accepts_github_tree_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(skills_engine, "USER_SKILLS_DIR", tmp_path / "user")
    monkeypatch.setattr(skills_engine, "SKILLS_CACHE_DIR", tmp_path / "cache")

    captured = _install_fake_engine(monkeypatch, stdout=json.dumps([]))

    url = (
        "https://github.com/alirezarezvani/claude-skills/tree/main/product-team/skills/spec-to-repo"
    )
    skills_engine.add(url, scope="user", cwd=tmp_path)

    command = captured["kwargs"]["command"]
    expected = "github:alirezarezvani/claude-skills//product-team/skills/spec-to-repo#main"
    assert expected in command
    assert url not in command


def test_add_rejects_missing_local_locator(tmp_path: Path) -> None:
    with pytest.raises(skills_engine.SkillsEngineError, match="Local skill locator not found"):
        skills_engine.add("./missing", scope="user", cwd=tmp_path)


def test_add_mounts_bare_relative_local_locator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "project"
    source = cwd / "skills" / "researcher"
    source.mkdir(parents=True)
    monkeypatch.setattr(skills_engine, "USER_SKILLS_DIR", tmp_path / "user")
    monkeypatch.setattr(skills_engine, "SKILLS_CACHE_DIR", tmp_path / "cache")

    captured = _install_fake_engine(monkeypatch, stdout=json.dumps([]))

    skills_engine.add("skills/researcher", scope="user", cwd=cwd)

    volumes = captured["kwargs"]["volumes"]
    assert volumes[str(source.resolve())] == {"bind": str(source.resolve()), "mode": "ro"}
    assert captured["kwargs"]["working_dir"] == str(cwd.resolve())
    # locator string reaches the engine unmodified
    assert "skills/researcher" in captured["kwargs"]["command"]


def test_add_mounts_current_directory_locator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "skill"
    source.mkdir()
    monkeypatch.setattr(skills_engine, "USER_SKILLS_DIR", tmp_path / "user")
    monkeypatch.setattr(skills_engine, "SKILLS_CACHE_DIR", tmp_path / "cache")

    captured = _install_fake_engine(monkeypatch, stdout=json.dumps([]))

    skills_engine.add(".", scope="user", cwd=source)

    volumes = captured["kwargs"]["volumes"]
    assert volumes[str(source.resolve())] == {"bind": str(source.resolve()), "mode": "ro"}
    assert "." in captured["kwargs"]["command"]


def test_add_does_not_mount_remote_locators(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(skills_engine, "USER_SKILLS_DIR", tmp_path / "user")
    monkeypatch.setattr(skills_engine, "SKILLS_CACHE_DIR", tmp_path / "cache")

    captured = _install_fake_engine(monkeypatch, stdout=json.dumps([]))

    remotes = (
        "github:org/repo",
        "npm:@acme/pkg",
        "https://git.example.com/x.git",
        "git@git.example.com:org/repo.git",
        "ftp://x/y",
    )
    for locator in remotes:
        skills_engine.add(locator, scope="user", cwd=tmp_path)
        assert not _ro_mounts(captured), locator
        assert captured["kwargs"]["working_dir"] is None, locator


def test_add_expands_tilde_local_locator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    source = home / "skills" / "foo"
    source.mkdir(parents=True)
    # POSIX expanduser reads HOME, ntpath.expanduser reads USERPROFILE.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(skills_engine, "USER_SKILLS_DIR", tmp_path / "user")
    monkeypatch.setattr(skills_engine, "SKILLS_CACHE_DIR", tmp_path / "cache")

    captured = _install_fake_engine(monkeypatch, stdout=json.dumps([]))

    skills_engine.add("~/skills/foo", scope="user", cwd=tmp_path)

    volumes = captured["kwargs"]["volumes"]
    assert volumes[str(source.resolve())] == {"bind": str(source.resolve()), "mode": "ro"}
    command = captured["kwargs"]["command"]
    # the engine receives the expanded absolute path, never a bare "~"
    assert str(home / "skills" / "foo") in command
    assert not any(arg.startswith("~") for arg in command)


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
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
            self.client.containers.create.side_effect = lambda image, **kwargs: _FakeContainer(
                json.dumps([]),
                "",
                0,
            )

        def pull_image(self, image: str, auto_clean: bool = False) -> None:
            pulled_images.append((image, auto_clean))

        def pull_if_newer(self, image: str, auto_clean: bool = False) -> bool:
            checked_images.append(image)
            return False

    monkeypatch.setattr(skills_engine, "DockerManager", FakeDockerManager)
    monkeypatch.setattr(skills_engine, "_skills_engine_checked", False)
    monkeypatch.setattr(skills_engine, "_manager", None)

    skills_engine.list_skills()

    assert (skills_engine.SKILLS_ENGINE_IMAGE, True) in pulled_images
    assert not checked_images


def test_run_engine_checks_updates_when_latest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
            self.client.containers.create.side_effect = lambda image, **kwargs: _FakeContainer(
                json.dumps([]),
                "",
                0,
            )

        def pull_image(self, image: str, auto_clean: bool = False) -> None:
            pulled_images.append((image, auto_clean))

        def pull_if_newer(self, image: str, auto_clean: bool = False) -> bool:
            checked_images.append((image, auto_clean))
            return False

    monkeypatch.setattr(skills_engine, "DockerManager", FakeDockerManager)
    monkeypatch.setattr(skills_engine, "_skills_engine_checked", False)
    monkeypatch.setattr(skills_engine, "_manager", None)
    monkeypatch.setattr(
        skills_engine,
        "get_config",
        lambda: {"auto_pull": True, "auto_clean": True},
    )

    skills_engine.list_skills()

    assert not pulled_images
    assert ("vibepod/skills-engine:latest", True) in checked_images
