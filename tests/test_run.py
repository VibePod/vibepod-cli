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

    workspace = tmp_path / "workspace"
    config_dir = tmp_path / "agents" / "auggie"
    augment_dir = config_dir / ".augment"
    workspace.mkdir(parents=True, exist_ok=True)
    augment_dir.mkdir(parents=True, exist_ok=True)

    manager.run_agent(
        agent="auggie",
        image="vibepod/auggie:latest",
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
        image="vibepod/devstral:latest",
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


def test_x11_volumes_and_env_returns_socket_and_display() -> None:
    volumes, env = run_cmd._x11_volumes_and_env(":0")
    assert ("/tmp/.X11-unix", "/tmp/.X11-unix", "rw") in volumes
    assert env == {"DISPLAY": ":0"}


def test_x11_volumes_and_env_preserves_display_value() -> None:
    volumes, env = run_cmd._x11_volumes_and_env(":1")
    assert env["DISPLAY"] == ":1"


def test_paste_images_flag_adds_x11_volumes_and_env(monkeypatch, tmp_path: Path) -> None:
    """--paste-images injects the X11 socket and DISPLAY into the container."""
    captured: dict = {}

    class _CapturingDockerManager:
        def ensure_network(self, name: str) -> None:
            pass

        def networks_with_running_containers(self) -> list[str]:
            return []

        def pull_image(self, image: str) -> None:
            pass

        def ensure_proxy(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def run_agent(self, **kwargs) -> object:  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            container = type(
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
            return container

    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(run_cmd, "get_config", lambda: _make_config())
    monkeypatch.setattr(run_cmd, "DockerManager", _CapturingDockerManager)

    run_cmd.run(agent="claude", workspace=tmp_path, detach=True, paste_images=True)

    assert ("/tmp/.X11-unix", "/tmp/.X11-unix", "rw") in captured.get(
        "extra_volumes", []
    ), f"X11 socket not found in extra_volumes: {captured.get('extra_volumes')}"


def test_paste_images_flag_warns_when_display_not_set(monkeypatch, tmp_path: Path) -> None:
    """--paste-images warns and skips X11 when DISPLAY is unset."""
    captured: dict = {}

    class _CapturingDockerManager:
        def ensure_network(self, name: str) -> None:
            pass

        def networks_with_running_containers(self) -> list[str]:
            return []

        def pull_image(self, image: str) -> None:
            pass

        def ensure_proxy(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def run_agent(self, **kwargs) -> object:  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            container = type(
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
            return container

    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(run_cmd, "get_config", lambda: _make_config())
    monkeypatch.setattr(run_cmd, "DockerManager", _CapturingDockerManager)

    run_cmd.run(agent="claude", workspace=tmp_path, detach=True, paste_images=True)

    x11_vols = [v for v in captured.get("extra_volumes", []) if "/tmp/.X11-unix" in str(v)]
    assert not x11_vols, "X11 socket should not be mounted when DISPLAY is unset"


def test_paste_images_false_does_not_add_x11(monkeypatch, tmp_path: Path) -> None:
    """Default (no --paste-images) does not inject X11 socket."""
    captured: dict = {}

    class _CapturingDockerManager:
        def ensure_network(self, name: str) -> None:
            pass

        def networks_with_running_containers(self) -> list[str]:
            return []

        def pull_image(self, image: str) -> None:
            pass

        def ensure_proxy(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def run_agent(self, **kwargs) -> object:  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            container = type(
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
            return container

    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(run_cmd, "get_config", lambda: _make_config())
    monkeypatch.setattr(run_cmd, "DockerManager", _CapturingDockerManager)

    run_cmd.run(agent="claude", workspace=tmp_path, detach=True, paste_images=False)

    x11_vols = [v for v in captured.get("extra_volumes", []) if "/tmp/.X11-unix" in str(v)]
    assert not x11_vols, "X11 socket should not be mounted when paste_images=False"


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

    def ensure_network(self, name: str) -> None:
        pass

    def pull_image(self, image: str) -> None:
        self.pulled.append(image)

    def ensure_proxy(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        pass

    def run_agent(self, **kwargs) -> object:  # type: ignore[no-untyped-def]
        return self._container

    def networks_with_running_containers(self) -> list[str]:
        return []


def _make_config(
    global_auto_pull: bool = False,
    agent_auto_pull: bool | None = None,
) -> dict:
    agent_cfg: dict = {"env": {}, "init": []}
    if agent_auto_pull is not None:
        agent_cfg["auto_pull"] = agent_auto_pull
    return {
        "default_agent": "claude",
        "auto_pull": global_auto_pull,
        "auto_remove": True,
        "network": "vibepod-network",
        "agents": {"claude": agent_cfg},
        "proxy": {"enabled": False},
        "logging": {"enabled": False},
    }


def test_auto_pull_global_triggers_pull(monkeypatch, tmp_path: Path) -> None:
    """Global auto_pull=true causes image pull on run."""
    stub = _StubDockerManager()
    monkeypatch.setattr(run_cmd, "get_config", lambda: _make_config(global_auto_pull=True))
    monkeypatch.setattr(run_cmd, "DockerManager", lambda: stub)

    run_cmd.run(agent="claude", workspace=tmp_path, detach=True)
    assert len(stub.pulled) == 1


def test_auto_pull_global_false_skips_pull(monkeypatch, tmp_path: Path) -> None:
    """Global auto_pull=false skips image pull."""
    stub = _StubDockerManager()
    monkeypatch.setattr(run_cmd, "get_config", lambda: _make_config(global_auto_pull=False))
    monkeypatch.setattr(run_cmd, "DockerManager", lambda: stub)

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
    monkeypatch.setattr(run_cmd, "DockerManager", lambda: stub)

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
    monkeypatch.setattr(run_cmd, "DockerManager", lambda: stub)

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
    monkeypatch.setattr(run_cmd, "DockerManager", lambda: stub)

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
    monkeypatch.setattr(run_cmd, "DockerManager", lambda: stub)

    run_cmd.run(agent="claude", workspace=tmp_path, detach=True)
    assert len(stub.pulled) == 1


def test_ikwid_appends_args_for_claude(monkeypatch, tmp_path: Path) -> None:
    """--ikwid appends --dangerously-skip-permissions to claude command."""
    captured: dict = {}

    class _CapturingDockerManager:
        def ensure_network(self, name: str) -> None:
            pass

        def networks_with_running_containers(self) -> list[str]:
            return []

        def pull_image(self, image: str) -> None:
            pass

        def ensure_proxy(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def run_agent(self, **kwargs) -> object:  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            container = type(
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
            return container

    monkeypatch.setattr(run_cmd, "get_config", lambda: _make_config())
    monkeypatch.setattr(run_cmd, "DockerManager", _CapturingDockerManager)

    run_cmd.run(agent="claude", workspace=tmp_path, detach=True, ikwid=True)

    assert captured["command"] == ["claude", "--dangerously-skip-permissions"]


def test_ikwid_appends_args_for_codex(monkeypatch, tmp_path: Path) -> None:
    """--ikwid appends --dangerously-bypass-approvals-and-sandbox to codex command."""
    captured: dict = {}

    class _CapturingDockerManager:
        def ensure_network(self, name: str) -> None:
            pass

        def networks_with_running_containers(self) -> list[str]:
            return []

        def pull_image(self, image: str) -> None:
            pass

        def ensure_proxy(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def run_agent(self, **kwargs) -> object:  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            container = type(
                "_Container",
                (),
                {
                    "name": "vibepod-codex-test",
                    "id": "abc123",
                    "status": "running",
                    "attrs": {"NetworkSettings": {"Networks": {}}},
                    "reload": lambda self: None,
                    "labels": {},
                    "logs": lambda self, **kw: b"",
                },
            )()
            return container

    cfg = _make_config()
    cfg["agents"]["codex"] = {"env": {}, "init": []}
    monkeypatch.setattr(run_cmd, "get_config", lambda: cfg)
    monkeypatch.setattr(run_cmd, "DockerManager", _CapturingDockerManager)

    run_cmd.run(agent="codex", workspace=tmp_path, detach=True, ikwid=True)

    assert captured["command"] == ["codex", "--dangerously-bypass-approvals-and-sandbox"]


def test_ikwid_appends_args_for_gemini(monkeypatch, tmp_path: Path) -> None:
    """--ikwid appends --approval-mode=yolo to gemini command."""
    captured: dict = {}

    class _CapturingDockerManager:
        def ensure_network(self, name: str) -> None:
            pass

        def networks_with_running_containers(self) -> list[str]:
            return []

        def pull_image(self, image: str) -> None:
            pass

        def ensure_proxy(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def run_agent(self, **kwargs) -> object:  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            container = type(
                "_Container",
                (),
                {
                    "name": "vibepod-gemini-test",
                    "id": "abc123",
                    "status": "running",
                    "attrs": {"NetworkSettings": {"Networks": {}}},
                    "reload": lambda self: None,
                    "labels": {},
                    "logs": lambda self, **kw: b"",
                },
            )()
            return container

    cfg = _make_config()
    cfg["agents"]["gemini"] = {"env": {}, "init": []}
    monkeypatch.setattr(run_cmd, "get_config", lambda: cfg)
    monkeypatch.setattr(run_cmd, "DockerManager", _CapturingDockerManager)

    run_cmd.run(agent="gemini", workspace=tmp_path, detach=True, ikwid=True)

    assert captured["command"] == [
        "env",
        "HOME=/config",
        "node",
        "/usr/local/bin/gemini",
        "--approval-mode=yolo",
    ]


def test_ikwid_appends_args_for_copilot(monkeypatch, tmp_path: Path) -> None:
    """--ikwid appends --yolo to copilot command."""
    captured: dict = {}

    class _CapturingDockerManager:
        def ensure_network(self, name: str) -> None:
            pass

        def networks_with_running_containers(self) -> list[str]:
            return []

        def pull_image(self, image: str) -> None:
            pass

        def ensure_proxy(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def run_agent(self, **kwargs) -> object:  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            container = type(
                "_Container",
                (),
                {
                    "name": "vibepod-copilot-test",
                    "id": "abc123",
                    "status": "running",
                    "attrs": {"NetworkSettings": {"Networks": {}}},
                    "reload": lambda self: None,
                    "labels": {},
                    "logs": lambda self, **kw: b"",
                },
            )()
            return container

    cfg = _make_config()
    cfg["agents"]["copilot"] = {"env": {}, "init": []}
    monkeypatch.setattr(run_cmd, "get_config", lambda: cfg)
    monkeypatch.setattr(run_cmd, "DockerManager", _CapturingDockerManager)

    run_cmd.run(agent="copilot", workspace=tmp_path, detach=True, ikwid=True)

    assert captured["command"] == ["copilot", "--yolo"]


def test_ikwid_appends_args_for_devstral(monkeypatch, tmp_path: Path) -> None:
    """--ikwid resolves devstral launch command and appends --auto-approve."""
    captured: dict = {}

    class _CapturingDockerManager:
        def ensure_network(self, name: str) -> None:
            pass

        def networks_with_running_containers(self) -> list[str]:
            return []

        def pull_image(self, image: str) -> None:
            pass

        def ensure_proxy(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def resolve_launch_command(self, image: str, command: list[str] | None) -> list[str]:
            assert image == "vibepod/devstral:latest"
            assert command is None
            return ["vibe"]

        def run_agent(self, **kwargs) -> object:  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            container = type(
                "_Container",
                (),
                {
                    "name": "vibepod-devstral-test",
                    "id": "abc123",
                    "status": "running",
                    "attrs": {"NetworkSettings": {"Networks": {}}},
                    "reload": lambda self: None,
                    "labels": {},
                    "logs": lambda self, **kw: b"",
                },
            )()
            return container

    cfg = _make_config()
    cfg["agents"]["devstral"] = {"env": {}, "init": []}
    monkeypatch.setattr(run_cmd, "get_config", lambda: cfg)
    monkeypatch.setattr(run_cmd, "DockerManager", _CapturingDockerManager)

    run_cmd.run(agent="devstral", workspace=tmp_path, detach=True, ikwid=True)

    assert captured["command"] == ["vibe", "--auto-approve"]


def test_ikwid_ignored_for_unsupported_agent(monkeypatch, tmp_path: Path) -> None:
    """--ikwid logs warning and proceeds for agents without ikwid_args."""
    captured: dict = {}

    class _CapturingDockerManager:
        def ensure_network(self, name: str) -> None:
            pass

        def networks_with_running_containers(self) -> list[str]:
            return []

        def pull_image(self, image: str) -> None:
            pass

        def ensure_proxy(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def run_agent(self, **kwargs) -> object:  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            container = type(
                "_Container",
                (),
                {
                    "name": "vibepod-opencode-test",
                    "id": "abc123",
                    "status": "running",
                    "attrs": {"NetworkSettings": {"Networks": {}}},
                    "reload": lambda self: None,
                    "labels": {},
                    "logs": lambda self, **kw: b"",
                },
            )()
            return container

    cfg = _make_config()
    cfg["agents"]["opencode"] = {"env": {}, "init": []}
    monkeypatch.setattr(run_cmd, "get_config", lambda: cfg)
    monkeypatch.setattr(run_cmd, "DockerManager", _CapturingDockerManager)

    run_cmd.run(agent="opencode", workspace=tmp_path, detach=True, ikwid=True)

    # Command should be unchanged (no ikwid args appended)
    assert captured["command"] == ["opencode"]


def test_ikwid_false_does_not_modify_command(monkeypatch, tmp_path: Path) -> None:
    """Without --ikwid, command is unchanged."""
    captured: dict = {}

    class _CapturingDockerManager:
        def ensure_network(self, name: str) -> None:
            pass

        def networks_with_running_containers(self) -> list[str]:
            return []

        def pull_image(self, image: str) -> None:
            pass

        def ensure_proxy(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def run_agent(self, **kwargs) -> object:  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            container = type(
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
            return container

    monkeypatch.setattr(run_cmd, "get_config", lambda: _make_config())
    monkeypatch.setattr(run_cmd, "DockerManager", _CapturingDockerManager)

    run_cmd.run(agent="claude", workspace=tmp_path, detach=True, ikwid=False)

    assert captured["command"] == ["claude"]


def test_llm_enabled_injects_openai_env_vars(monkeypatch, tmp_path: Path) -> None:
    """When llm.enabled=true, OPENAI_BASE_URL/API_KEY/MODEL are injected."""
    captured: dict = {}

    class _CapturingDockerManager:
        def ensure_network(self, name: str) -> None:
            pass

        def networks_with_running_containers(self) -> list[str]:
            return []

        def pull_image(self, image: str) -> None:
            pass

        def ensure_proxy(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def run_agent(self, **kwargs) -> object:  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            container = type(
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
            return container

    cfg = _make_config()
    cfg["llm"] = {
        "enabled": True,
        "base_url": "http://localhost:11434/v1",
        "api_key": "sk-test-key",
        "model": "llama3",
    }
    monkeypatch.setattr(run_cmd, "get_config", lambda: cfg)
    monkeypatch.setattr(run_cmd, "DockerManager", _CapturingDockerManager)

    run_cmd.run(agent="claude", workspace=tmp_path, detach=True)

    env = captured["env"]
    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:11434/v1"
    assert env["ANTHROPIC_API_KEY"] == "sk-test-key"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-test-key"
    assert env["ANTHROPIC_MODEL"] == "llama3"
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "llama3"
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "llama3"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "llama3"
    assert captured["command"] == ["claude", "--model", "llama3"]


def test_llm_enabled_injects_openai_env_vars_for_codex(monkeypatch, tmp_path: Path) -> None:
    """When llm.enabled=true, codex gets OPENAI_* env vars."""
    captured: dict = {}

    class _CapturingDockerManager:
        def ensure_network(self, name: str) -> None:
            pass

        def networks_with_running_containers(self) -> list[str]:
            return []

        def pull_image(self, image: str) -> None:
            pass

        def ensure_proxy(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def run_agent(self, **kwargs) -> object:  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            container = type(
                "_Container",
                (),
                {
                    "name": "vibepod-codex-test",
                    "id": "abc123",
                    "status": "running",
                    "attrs": {"NetworkSettings": {"Networks": {}}},
                    "reload": lambda self: None,
                    "labels": {},
                    "logs": lambda self, **kw: b"",
                },
            )()
            return container

    cfg = _make_config()
    cfg["agents"]["codex"] = {"env": {}, "init": []}
    cfg["llm"] = {
        "enabled": True,
        "base_url": "http://localhost:11434/v1",
        "api_key": "sk-test-key",
        "model": "llama3",
    }
    monkeypatch.setattr(run_cmd, "get_config", lambda: cfg)
    monkeypatch.setattr(run_cmd, "DockerManager", _CapturingDockerManager)

    run_cmd.run(agent="codex", workspace=tmp_path, detach=True)

    env = captured["env"]
    assert env["CODEX_OSS_BASE_URL"] == "http://localhost:11434/v1"
    assert "OPENAI_API_KEY" not in env
    assert captured["command"] == ["codex", "--oss", "-m", "llama3"]


def test_llm_disabled_does_not_inject_env_vars(monkeypatch, tmp_path: Path) -> None:
    """When llm.enabled=false, no LLM env vars are injected."""
    captured: dict = {}

    class _CapturingDockerManager:
        def ensure_network(self, name: str) -> None:
            pass

        def networks_with_running_containers(self) -> list[str]:
            return []

        def pull_image(self, image: str) -> None:
            pass

        def ensure_proxy(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def run_agent(self, **kwargs) -> object:  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            container = type(
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
            return container

    cfg = _make_config()
    cfg["llm"] = {
        "enabled": False,
        "base_url": "http://localhost:11434/v1",
        "api_key": "sk-test-key",
        "model": "llama3",
    }
    monkeypatch.setattr(run_cmd, "get_config", lambda: cfg)
    monkeypatch.setattr(run_cmd, "DockerManager", _CapturingDockerManager)

    run_cmd.run(agent="claude", workspace=tmp_path, detach=True)

    env = captured["env"]
    assert "ANTHROPIC_BASE_URL" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL" not in env
    assert "ANTHROPIC_DEFAULT_SONNET_MODEL" not in env
    assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" not in env
    assert "ANTHROPIC_MODEL" not in env


def test_llm_skipped_for_agent_without_mapping(monkeypatch, tmp_path: Path) -> None:
    """Agents without llm_env_map get no LLM env vars even when enabled."""
    captured: dict = {}

    class _CapturingDockerManager:
        def ensure_network(self, name: str) -> None:
            pass

        def networks_with_running_containers(self) -> list[str]:
            return []

        def pull_image(self, image: str) -> None:
            pass

        def ensure_proxy(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def run_agent(self, **kwargs) -> object:  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            container = type(
                "_Container",
                (),
                {
                    "name": "vibepod-gemini-test",
                    "id": "abc123",
                    "status": "running",
                    "attrs": {"NetworkSettings": {"Networks": {}}},
                    "reload": lambda self: None,
                    "labels": {},
                    "logs": lambda self, **kw: b"",
                },
            )()
            return container

    cfg = _make_config()
    cfg["agents"]["gemini"] = {"env": {}, "init": []}
    cfg["llm"] = {
        "enabled": True,
        "base_url": "http://localhost:11434/v1",
        "api_key": "sk-test",
        "model": "llama3",
    }
    monkeypatch.setattr(run_cmd, "get_config", lambda: cfg)
    monkeypatch.setattr(run_cmd, "DockerManager", _CapturingDockerManager)

    run_cmd.run(agent="gemini", workspace=tmp_path, detach=True)

    env = captured["env"]
    assert "ANTHROPIC_BASE_URL" not in env
    assert "OPENAI_BASE_URL" not in env


def test_llm_empty_model_not_injected(monkeypatch, tmp_path: Path) -> None:
    """When llm.model is empty, model env var is not set."""
    captured: dict = {}

    class _CapturingDockerManager:
        def ensure_network(self, name: str) -> None:
            pass

        def networks_with_running_containers(self) -> list[str]:
            return []

        def pull_image(self, image: str) -> None:
            pass

        def ensure_proxy(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def run_agent(self, **kwargs) -> object:  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            container = type(
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
            return container

    cfg = _make_config()
    cfg["llm"] = {
        "enabled": True,
        "base_url": "http://localhost:11434/v1",
        "api_key": "sk-test",
        "model": "",
    }
    monkeypatch.setattr(run_cmd, "get_config", lambda: cfg)
    monkeypatch.setattr(run_cmd, "DockerManager", _CapturingDockerManager)

    run_cmd.run(agent="claude", workspace=tmp_path, detach=True)

    env = captured["env"]
    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:11434/v1"
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL" not in env
    assert "ANTHROPIC_DEFAULT_SONNET_MODEL" not in env
    assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" not in env
    assert captured["command"] == ["claude"]


def test_llm_per_agent_env_overrides_llm(monkeypatch, tmp_path: Path) -> None:
    """Per-agent env settings take precedence over LLM injection."""
    captured: dict = {}

    class _CapturingDockerManager:
        def ensure_network(self, name: str) -> None:
            pass

        def networks_with_running_containers(self) -> list[str]:
            return []

        def pull_image(self, image: str) -> None:
            pass

        def ensure_proxy(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def run_agent(self, **kwargs) -> object:  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            container = type(
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
            return container

    cfg = _make_config()
    cfg["agents"]["claude"]["env"] = {"ANTHROPIC_BASE_URL": "http://custom:9999/v1"}
    cfg["llm"] = {
        "enabled": True,
        "base_url": "http://localhost:11434/v1",
        "api_key": "sk-test",
        "model": "llama3",
    }
    monkeypatch.setattr(run_cmd, "get_config", lambda: cfg)
    monkeypatch.setattr(run_cmd, "DockerManager", _CapturingDockerManager)

    run_cmd.run(agent="claude", workspace=tmp_path, detach=True)

    env = captured["env"]
    assert env["ANTHROPIC_BASE_URL"] == "http://custom:9999/v1"


def test_run_accepts_short_agent_name(monkeypatch, tmp_path: Path) -> None:
    class _UnavailableDockerManager:
        def __init__(self) -> None:
            raise DockerClientError("Docker unavailable")

    monkeypatch.setattr(
        run_cmd,
        "get_config",
        lambda: {"default_agent": "claude", "agents": {"claude": {"env": {}}}},
    )
    monkeypatch.setattr(run_cmd, "DockerManager", _UnavailableDockerManager)

    with pytest.raises(typer.Exit) as exc:
        run_cmd.run(agent="c", workspace=tmp_path)

    assert exc.value.exit_code == EXIT_DOCKER_NOT_RUNNING


def test_run_forwards_host_terminal_env(monkeypatch, tmp_path: Path) -> None:
    captured: dict = {}

    class _CapturingDockerManager:
        def ensure_network(self, name: str) -> None:
            pass

        def networks_with_running_containers(self) -> list[str]:
            return []

        def pull_image(self, image: str) -> None:
            pass

        def ensure_proxy(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def run_agent(self, **kwargs) -> object:  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            container = type(
                "_Container",
                (),
                {
                    "name": "vibepod-gemini-test",
                    "id": "abc123",
                    "status": "running",
                    "attrs": {"NetworkSettings": {"Networks": {}}},
                    "reload": lambda self: None,
                    "labels": {},
                    "logs": lambda self, **kw: b"",
                },
            )()
            return container

    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setenv("TERM_PROGRAM", "vscode")
    monkeypatch.setenv("TERM_PROGRAM_VERSION", "1.100.0")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setattr(run_cmd, "get_config", lambda: _make_config())
    monkeypatch.setattr(run_cmd, "DockerManager", _CapturingDockerManager)

    run_cmd.run(agent="gemini", workspace=tmp_path, detach=True)

    env = captured["env"]
    assert env["TERM"] == "xterm-256color"
    assert env["COLORTERM"] == "truecolor"
    assert env["TERM_PROGRAM"] == "vscode"
    assert env["TERM_PROGRAM_VERSION"] == "1.100.0"
    assert env["LANG"] == "en_US.UTF-8"


def test_run_sets_default_term_when_host_term_missing(monkeypatch, tmp_path: Path) -> None:
    captured: dict = {}

    class _CapturingDockerManager:
        def ensure_network(self, name: str) -> None:
            pass

        def networks_with_running_containers(self) -> list[str]:
            return []

        def pull_image(self, image: str) -> None:
            pass

        def ensure_proxy(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def run_agent(self, **kwargs) -> object:  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            container = type(
                "_Container",
                (),
                {
                    "name": "vibepod-gemini-test",
                    "id": "abc123",
                    "status": "running",
                    "attrs": {"NetworkSettings": {"Networks": {}}},
                    "reload": lambda self: None,
                    "labels": {},
                    "logs": lambda self, **kw: b"",
                },
            )()
            return container

    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.delenv("COLORTERM", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    monkeypatch.delenv("TERM_PROGRAM_VERSION", raising=False)
    monkeypatch.delenv("LANG", raising=False)
    monkeypatch.setattr(run_cmd, "get_config", lambda: _make_config())
    monkeypatch.setattr(run_cmd, "DockerManager", _CapturingDockerManager)

    run_cmd.run(agent="gemini", workspace=tmp_path, detach=True)

    env = captured["env"]
    assert env["TERM"] == "xterm-256color"
