"""Run command and Docker mount behavior tests."""

from __future__ import annotations

from pathlib import Path

from vibepod.commands import run as run_cmd
from vibepod.core.docker import DockerManager


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
