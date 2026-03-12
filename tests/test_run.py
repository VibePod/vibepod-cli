"""Run command and Docker mount behavior tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from vibepod.commands import run as run_cmd
from vibepod.constants import EXIT_DOCKER_NOT_RUNNING
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
    manager.runtime = "docker"

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
    manager.runtime = "docker"

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


def test_run_agent_forwards_userns_mode(tmp_path: Path) -> None:
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
    manager.runtime = "podman"

    workspace = tmp_path / "workspace"
    config_dir = tmp_path / "agents" / "claude"
    workspace.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)

    manager.run_agent(
        agent="claude",
        image="vibepod/claude:latest",
        workspace=workspace,
        config_dir=config_dir,
        config_mount_path="/claude",
        env={},
        command=["claude"],
        auto_remove=True,
        name=None,
        version="0.2.1",
        network="vibepod-network",
        userns_mode="keep-id",
    )

    run_kwargs = manager.client.containers.run_kwargs  # type: ignore[union-attr]
    assert run_kwargs is not None
    assert run_kwargs["userns_mode"] == "keep-id"


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
    manager.runtime = "docker"

    workspace = tmp_path / "workspace"
    config_dir = tmp_path / "agents" / "claude"
    workspace.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)

    manager.run_agent(
        agent="claude",
        image="vibepod/claude:latest",
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


class _StubDockerManager:
    """Minimal DockerManager stub that records pull_image calls."""

    def __init__(self) -> None:
        self.pulled: list[str] = []
        self.runtime = "docker"
        self.ensure_proxy_kwargs: dict | None = None
        self.run_agent_kwargs: dict | None = None
        self._container = type(
            "_Container",
            (),
            {
                "name": "vibepod-claude-test",
                "id": "abc123",
                "status": "running",
                "attrs": {"NetworkSettings": {"Networks": {}}},
                "reload": lambda self: None,
                "labels": {},
                "logs": lambda self, **kw: b"",
            },
        )()
        self._proxy = type(
            "_Proxy",
            (),
            {
                "name": "vibepod-proxy",
                "status": "running",
                "attrs": {
                    "NetworkSettings": {
                        "Networks": {
                            "vibepod-network": {
                                "IPAddress": "172.18.0.2",
                            }
                        }
                    }
                },
                "reload": lambda self: None,
            },
        )()

    def ensure_network(self, name: str) -> None:
        pass

    def pull_image(self, image: str) -> None:
        self.pulled.append(image)

    def ensure_proxy(self, **kwargs) -> object:  # type: ignore[no-untyped-def]
        self.ensure_proxy_kwargs = kwargs
        return self._proxy

    def run_agent(self, **kwargs) -> object:  # type: ignore[no-untyped-def]
        self.run_agent_kwargs = kwargs
        return self._container

    def networks_with_running_containers(self) -> list[str]:
        return []


def _make_config(
    global_auto_pull: bool = False,
    agent_auto_pull: bool | None = None,
    container_userns_mode: str | None = None,
) -> dict:
    agent_cfg: dict = {"env": {}, "init": []}
    if agent_auto_pull is not None:
        agent_cfg["auto_pull"] = agent_auto_pull
    return {
        "default_agent": "claude",
        "auto_pull": global_auto_pull,
        "auto_remove": True,
        "container_userns_mode": container_userns_mode,
        "network": "vibepod-network",
        "agents": {"claude": agent_cfg},
        "proxy": {"enabled": False},
        "logging": {"enabled": False},
    }


def test_auto_pull_global_triggers_pull(monkeypatch, tmp_path: Path) -> None:
    """Global auto_pull=true causes image pull on run."""
    stub = _StubDockerManager()
    monkeypatch.setattr(run_cmd, "get_config", lambda: _make_config(global_auto_pull=True))
    monkeypatch.setattr(run_cmd, "get_manager", lambda **kwargs: stub)

    run_cmd.run(agent="claude", workspace=tmp_path, detach=True)
    assert len(stub.pulled) == 1


def test_auto_pull_global_false_skips_pull(monkeypatch, tmp_path: Path) -> None:
    """Global auto_pull=false skips image pull."""
    stub = _StubDockerManager()
    monkeypatch.setattr(run_cmd, "get_config", lambda: _make_config(global_auto_pull=False))
    monkeypatch.setattr(run_cmd, "get_manager", lambda **kwargs: stub)

    run_cmd.run(agent="claude", workspace=tmp_path, detach=True)
    assert stub.pulled == []


def test_auto_pull_per_agent_true_overrides_global_false(monkeypatch, tmp_path: Path) -> None:
    """Per-agent auto_pull=true overrides global auto_pull=false."""
    stub = _StubDockerManager()
    monkeypatch.setattr(
        run_cmd,
        "get_config",
        lambda: _make_config(global_auto_pull=False, agent_auto_pull=True),
    )
    monkeypatch.setattr(run_cmd, "get_manager", lambda **kwargs: stub)

    run_cmd.run(agent="claude", workspace=tmp_path, detach=True)
    assert len(stub.pulled) == 1


def test_auto_pull_per_agent_false_overrides_global_true(monkeypatch, tmp_path: Path) -> None:
    """Per-agent auto_pull=false overrides global auto_pull=true."""
    stub = _StubDockerManager()
    monkeypatch.setattr(
        run_cmd,
        "get_config",
        lambda: _make_config(global_auto_pull=True, agent_auto_pull=False),
    )
    monkeypatch.setattr(run_cmd, "get_manager", lambda **kwargs: stub)

    run_cmd.run(agent="claude", workspace=tmp_path, detach=True)
    assert stub.pulled == []


def test_auto_pull_cli_flag_overrides_config(monkeypatch, tmp_path: Path) -> None:
    """--pull flag forces pull even when config disables it."""
    stub = _StubDockerManager()
    monkeypatch.setattr(
        run_cmd,
        "get_config",
        lambda: _make_config(global_auto_pull=False, agent_auto_pull=False),
    )
    monkeypatch.setattr(run_cmd, "get_manager", lambda **kwargs: stub)

    run_cmd.run(agent="claude", workspace=tmp_path, detach=True, pull=True)
    assert len(stub.pulled) == 1


def test_auto_pull_per_agent_none_falls_back_to_global(monkeypatch, tmp_path: Path) -> None:
    """Per-agent auto_pull=None (unset) falls back to global setting."""
    stub = _StubDockerManager()
    monkeypatch.setattr(
        run_cmd,
        "get_config",
        lambda: _make_config(global_auto_pull=True, agent_auto_pull=None),
    )
    monkeypatch.setattr(run_cmd, "get_manager", lambda **kwargs: stub)

    run_cmd.run(agent="claude", workspace=tmp_path, detach=True)
    assert len(stub.pulled) == 1


def test_run_accepts_short_agent_name(monkeypatch, tmp_path: Path) -> None:
    def _unavailable_get_manager(**kwargs):
        raise DockerClientError("Docker unavailable")

    monkeypatch.setattr(
        run_cmd,
        "get_config",
        lambda: {"default_agent": "claude", "agents": {"claude": {"env": {}}}},
    )
    monkeypatch.setattr(run_cmd, "get_manager", _unavailable_get_manager)

    with pytest.raises(typer.Exit) as exc:
        run_cmd.run(agent="c", workspace=tmp_path)

    assert exc.value.exit_code == EXIT_DOCKER_NOT_RUNNING


def test_run_passes_configured_userns_mode(monkeypatch, tmp_path: Path) -> None:
    stub = _StubDockerManager()
    monkeypatch.setattr(
        run_cmd,
        "get_config",
        lambda: _make_config(container_userns_mode="keep-id"),
    )
    monkeypatch.setattr(run_cmd, "get_manager", lambda **kwargs: stub)

    run_cmd.run(agent="claude", workspace=tmp_path, detach=True)

    assert stub.run_agent_kwargs is not None
    assert stub.run_agent_kwargs["userns_mode"] == "keep-id"


def test_run_cli_userns_overrides_config(monkeypatch, tmp_path: Path) -> None:
    stub = _StubDockerManager()
    monkeypatch.setattr(
        run_cmd,
        "get_config",
        lambda: _make_config(container_userns_mode="host"),
    )
    monkeypatch.setattr(run_cmd, "get_manager", lambda **kwargs: stub)

    run_cmd.run(agent="claude", workspace=tmp_path, detach=True, userns="keep-id")

    assert stub.run_agent_kwargs is not None
    assert stub.run_agent_kwargs["userns_mode"] == "keep-id"


def test_run_uses_proxy_container_ip_for_proxy_env(monkeypatch, tmp_path: Path) -> None:
    stub = _StubDockerManager()
    monkeypatch.setattr(
        run_cmd,
        "get_config",
        lambda: {
            **_make_config(),
            "proxy": {
                "enabled": True,
                "image": "vibepod/proxy:latest",
                "db_path": str(tmp_path / "proxy" / "proxy.db"),
            },
        },
    )
    monkeypatch.setattr(run_cmd, "get_manager", lambda **kwargs: stub)

    run_cmd.run(agent="claude", workspace=tmp_path, detach=True)

    assert stub.run_agent_kwargs is not None
    env = stub.run_agent_kwargs["env"]
    assert env["HTTP_PROXY"] == "http://172.18.0.2:8080"
    assert env["HTTPS_PROXY"] == "http://172.18.0.2:8080"
