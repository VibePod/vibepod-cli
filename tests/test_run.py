"""Run command and Docker mount behavior tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from vibepod.commands import run as run_cmd
from vibepod.core.docker import DockerClientError, DockerManager


def test_agent_extra_volumes_for_auggie(tmp_path: Path) -> None:
    config_dir = tmp_path / "agents" / "auggie"
    augment_dir = config_dir / ".augment"

    assert run_cmd._agent_extra_volumes("auggie", config_dir) == [
        (str(augment_dir), "/root/.augment", "rw"),
        (str(augment_dir), "/home/node/.augment", "rw"),
    ]


def test_agent_extra_volumes_for_other_agents(tmp_path: Path) -> None:
    config_dir = tmp_path / "agents" / "claude"
    assert run_cmd._agent_extra_volumes("claude", config_dir) == []


def test_agent_extra_volumes_for_copilot(tmp_path: Path) -> None:
    config_dir = tmp_path / "agents" / "copilot"
    config_host = config_dir / ".copilot"

    assert run_cmd._agent_extra_volumes("copilot", config_dir) == [
        (str(config_host), "/root/.copilot", "rw"),
        (str(config_host), "/home/node/.copilot", "rw"),
        (str(config_host), "/home/coder/.copilot", "rw"),
    ]


def test_run_agent_supports_duplicate_host_mounts(tmp_path: Path) -> None:
    class _FakeContainers:
        def __init__(self) -> None:
            self.run_kwargs: dict | None = None

        def run(self, **kwargs):
            self.run_kwargs = kwargs
            return {"id": "agent"}

    class _FakeClient:
        def __init__(self) -> None:
            self.containers = _FakeContainers()

    manager = object.__new__(DockerManager)
    manager.client = _FakeClient()  # type: ignore[assignment]

    workspace = tmp_path / "workspace"
    config_dir = tmp_path / "agents" / "auggie"
    augment_dir = config_dir / ".augment"
    workspace.mkdir(parents=True, exist_ok=True)
    augment_dir.mkdir(parents=True, exist_ok=True)

    manager.run_agent(
        agent="auggie",
        image="nezhar/auggie-cli:latest",
        workspace=workspace,
        config_dir=config_dir,
        config_mount_path="/config",
        env={},
        command=["auggie"],
        auto_remove=True,
        name=None,
        version="0.2.1",
        network="vibepod-network",
        extra_volumes=[
            (str(augment_dir), "/root/.augment", "rw"),
            (str(augment_dir), "/home/node/.augment", "rw"),
        ],
    )

    run_kwargs = manager.client.containers.run_kwargs  # type: ignore[union-attr]
    assert run_kwargs is not None
    volumes = run_kwargs["volumes"]
    assert f"{workspace}:/workspace:rw" in volumes
    assert f"{config_dir}:/config:rw" in volumes
    assert f"{augment_dir}:/root/.augment:rw" in volumes
    assert f"{augment_dir}:/home/node/.augment:rw" in volumes


def test_run_agent_forwards_platform_and_user(tmp_path: Path) -> None:
    class _FakeContainers:
        def __init__(self) -> None:
            self.run_kwargs: dict | None = None

        def run(self, **kwargs):
            self.run_kwargs = kwargs
            return {"id": "agent"}

    class _FakeClient:
        def __init__(self) -> None:
            self.containers = _FakeContainers()

    manager = object.__new__(DockerManager)
    manager.client = _FakeClient()  # type: ignore[assignment]

    workspace = tmp_path / "workspace"
    config_dir = tmp_path / "agents" / "devstral"
    workspace.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)

    manager.run_agent(
        agent="devstral",
        image="nezhar/devstral-cli:latest",
        workspace=workspace,
        config_dir=config_dir,
        config_mount_path="/config",
        env={},
        command=None,
        auto_remove=True,
        name=None,
        version="0.2.1",
        network="vibepod-network",
        platform="linux/amd64",
        user="1000:1000",
    )

    run_kwargs = manager.client.containers.run_kwargs  # type: ignore[union-attr]
    assert run_kwargs is not None
    assert run_kwargs["platform"] == "linux/amd64"
    assert run_kwargs["user"] == "1000:1000"


def test_run_agent_forwards_entrypoint(tmp_path: Path) -> None:
    class _FakeContainers:
        def __init__(self) -> None:
            self.run_kwargs: dict | None = None

        def run(self, **kwargs):
            self.run_kwargs = kwargs
            return {"id": "agent"}

    class _FakeClient:
        def __init__(self) -> None:
            self.containers = _FakeContainers()

    manager = object.__new__(DockerManager)
    manager.client = _FakeClient()  # type: ignore[assignment]

    workspace = tmp_path / "workspace"
    config_dir = tmp_path / "agents" / "claude"
    workspace.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)

    manager.run_agent(
        agent="claude",
        image="nezhar/claude-container:latest",
        workspace=workspace,
        config_dir=config_dir,
        config_mount_path="/claude",
        env={},
        command=["claude"],
        auto_remove=True,
        name=None,
        version="0.2.1",
        network="vibepod-network",
        entrypoint=["/bin/sh", "-lc", 'echo "init"; exec "$@"', "--"],
    )

    run_kwargs = manager.client.containers.run_kwargs  # type: ignore[union-attr]
    assert run_kwargs is not None
    assert run_kwargs["entrypoint"] == ["/bin/sh", "-lc", 'echo "init"; exec "$@"', "--"]


def test_agent_init_commands_from_list() -> None:
    commands = run_cmd._agent_init_commands("claude", {"init": ["apk add --no-cache ripgrep"]})
    assert commands == ["apk add --no-cache ripgrep"]


def test_agent_init_commands_from_string() -> None:
    commands = run_cmd._agent_init_commands("claude", {"init": "apk add --no-cache ripgrep"})
    assert commands == ["apk add --no-cache ripgrep"]


def test_agent_init_commands_invalid_type() -> None:
    with pytest.raises(typer.BadParameter):
        run_cmd._agent_init_commands("claude", {"init": {"run": "echo hi"}})


def test_agent_init_commands_invalid_item_type() -> None:
    with pytest.raises(typer.BadParameter):
        run_cmd._agent_init_commands("claude", {"init": ["echo hi", 123]})


def test_init_entrypoint_contains_commands() -> None:
    entrypoint = run_cmd._init_entrypoint(["apk add --no-cache ripgrep", "npm install -g cowsay"])
    assert entrypoint[:2] == ["/bin/sh", "-lc"]
    assert entrypoint[-1] == "--"
    script = entrypoint[2]
    assert "set -e" in script
    assert "apk add --no-cache ripgrep" in script
    assert "npm install -g cowsay" in script
    assert 'exec "$@"' in script


def test_resolve_launch_command_uses_image_defaults_when_no_override() -> None:
    class _FakeImage:
        attrs = {"Config": {"Entrypoint": ["/usr/local/bin/entry"], "Cmd": ["agent", "--help"]}}

    class _FakeImages:
        def get(self, image: str):
            assert image == "example/image:latest"
            return _FakeImage()

    class _FakeClient:
        def __init__(self) -> None:
            self.images = _FakeImages()

    manager = object.__new__(DockerManager)
    manager.client = _FakeClient()  # type: ignore[assignment]

    launch = manager.resolve_launch_command("example/image:latest", None)
    assert launch == ["/usr/local/bin/entry", "agent", "--help"]


def test_resolve_launch_command_applies_override() -> None:
    class _FakeImage:
        attrs = {"Config": {"Entrypoint": ["/usr/local/bin/entry"], "Cmd": ["agent"]}}

    class _FakeImages:
        def get(self, image: str):
            assert image == "example/image:latest"
            return _FakeImage()

    class _FakeClient:
        def __init__(self) -> None:
            self.images = _FakeImages()

    manager = object.__new__(DockerManager)
    manager.client = _FakeClient()  # type: ignore[assignment]

    launch = manager.resolve_launch_command("example/image:latest", ["custom", "--version"])
    assert launch == ["/usr/local/bin/entry", "custom", "--version"]


def test_resolve_launch_command_requires_non_empty_process() -> None:
    class _FakeImage:
        attrs = {"Config": {}}

    class _FakeImages:
        def get(self, image: str):
            assert image == "example/image:latest"
            return _FakeImage()

    class _FakeClient:
        def __init__(self) -> None:
            self.images = _FakeImages()

    manager = object.__new__(DockerManager)
    manager.client = _FakeClient()  # type: ignore[assignment]

    with pytest.raises(DockerClientError):
        manager.resolve_launch_command("example/image:latest", None)
