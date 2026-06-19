"""Run command and Docker mount behavior tests."""

from __future__ import annotations

import builtins
import importlib
import json
import sys
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from vibepod.cli import app
from vibepod.commands import run as run_cmd
from vibepod.constants import EXIT_DOCKER_NOT_RUNNING, SUPPORTED_AGENTS
from vibepod.core import skills_engine
from vibepod.core.docker import DockerClientError, DockerManager

# ---------------------------------------------------------------------------
# Autouse fixture: allow workspace dirs by default so existing tests still pass
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _allow_all_dirs(monkeypatch):
    """Patch is_dir_allowed to return True so permission prompts don't break unrelated tests."""
    monkeypatch.setattr(run_cmd, "is_dir_allowed", lambda p: True)


def test_docker_module_imports_without_posix_tty_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native Windows lacks termios/tty, but the CLI must still import."""
    import vibepod.core as core_pkg

    original_attr = getattr(core_pkg, "docker", None)
    original_module = sys.modules.pop("vibepod.core.docker", None)
    original_import = builtins.__import__

    def import_without_posix_tty(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name in {"termios", "tty"}:
            raise ModuleNotFoundError(f"No module named '{name}'")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_without_posix_tty)
    try:
        imported = importlib.import_module("vibepod.core.docker")
    finally:
        sys.modules.pop("vibepod.core.docker", None)
        if original_module is not None:
            sys.modules["vibepod.core.docker"] = original_module
        if original_attr is not None:
            core_pkg.docker = original_attr

    assert imported.DockerManager is not None


def test_host_identity_env_empty_without_posix_user_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(run_cmd.os, "getuid", raising=False)
    monkeypatch.delattr(run_cmd.os, "getgid", raising=False)

    assert run_cmd._host_identity_env() == {}


def test_agent_extra_volumes_for_auggie(tmp_path: Path) -> None:
    config_dir = tmp_path / "agents" / "auggie"
    augment_dir = config_dir / ".augment"

    assert run_cmd._agent_extra_volumes("auggie", config_dir) == [
        (str(augment_dir), "/root/.augment", "rw"),
        (str(augment_dir), "/home/node/.augment", "rw"),
    ]


def test_agent_extra_volumes_for_other_agents(tmp_path: Path) -> None:
    config_dir = tmp_path / "agents" / "claude"
    # Agents without explicit volume mappings return empty
    for agent in ("claude", "gemini", "codex", "devstral", "pi", "agy"):
        assert run_cmd._agent_extra_volumes(agent, config_dir) == []


def test_agent_extra_volumes_for_copilot(tmp_path: Path) -> None:
    config_dir = tmp_path / "agents" / "copilot"
    config_host = config_dir / ".copilot"

    assert run_cmd._agent_extra_volumes("copilot", config_dir) == [
        (str(config_host), "/root/.copilot", "rw"),
        (str(config_host), "/home/node/.copilot", "rw"),
        (str(config_host), "/home/coder/.copilot", "rw"),
    ]


def test_agent_extra_volumes_for_opencode(tmp_path: Path) -> None:
    config_dir = tmp_path / "agents" / "opencode"
    data_dir = config_dir / ".local" / "share" / "opencode"
    xdg_config_dir = config_dir / ".config" / "opencode"

    assert run_cmd._agent_extra_volumes("opencode", config_dir) == [
        (str(data_dir), "/root/.local/share/opencode", "rw"),
        (str(xdg_config_dir), "/root/.config/opencode", "rw"),
        (str(data_dir), "/home/node/.local/share/opencode", "rw"),
        (str(xdg_config_dir), "/home/node/.config/opencode", "rw"),
    ]


def test_skills_mounts_for_agent_ignores_malformed_lockfiles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    local_root = tmp_path / "local-skills"
    user_root = tmp_path / "user-skills"
    local_root.mkdir()
    user_root.mkdir()
    (local_root / "skills-lock.json").write_text(json.dumps({"skills": []}), encoding="utf-8")
    (user_root / "skills-lock.json").write_text(json.dumps([]), encoding="utf-8")

    monkeypatch.setattr(skills_engine, "local_skills_dir", lambda workspace: local_root)
    monkeypatch.setattr(skills_engine, "user_skills_dir", lambda: user_root)

    assert run_cmd._skills_mounts_for_agent("codex", tmp_path) == []


def test_skills_mounts_for_agent_requires_skill_directories_under_scope_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    local_root = tmp_path / "local-skills"
    user_root = tmp_path / "user-skills"
    outside_root = tmp_path / "outside"
    valid_skill = local_root / "installed" / "valid"
    file_skill = local_root / "installed" / "file-skill"
    symlink_skill = local_root / "installed" / "symlink-skill"
    for path in (local_root, user_root, outside_root, valid_skill):
        path.mkdir(parents=True)
    file_skill.write_text("not a directory", encoding="utf-8")
    symlink_skill.symlink_to(outside_root, target_is_directory=True)
    (local_root / "skills-lock.json").write_text(
        json.dumps(
            {
                "skills": {
                    "valid": {"path": "installed/valid"},
                    "valid-name_2": {"path": "installed/valid"},
                    "bad.name": {"path": "installed/valid"},
                    "Upper": {"path": "installed/valid"},
                    "../config": {"path": "installed/valid"},
                    "nested/name": {"path": "installed/valid"},
                    "nested\\name": {"path": "installed/valid"},
                    "bad..name": {"path": "installed/valid"},
                    "traversal": {"path": "../outside"},
                    "absolute": {"path": str(outside_root)},
                    "file": {"path": "installed/file-skill"},
                    "symlink": {"path": "installed/symlink-skill"},
                }
            }
        ),
        encoding="utf-8",
    )
    (user_root / "skills-lock.json").write_text(json.dumps({"skills": {}}), encoding="utf-8")

    monkeypatch.setattr(skills_engine, "local_skills_dir", lambda workspace: local_root)
    monkeypatch.setattr(skills_engine, "user_skills_dir", lambda: user_root)

    assert run_cmd._skills_mounts_for_agent("codex", tmp_path) == [
        (str(valid_skill.resolve()), "/config/.agents/skills/valid", "ro"),
        (str(valid_skill.resolve()), "/config/.agents/skills/valid-name_2", "ro"),
    ]


def test_skills_mounts_for_pi_use_agent_dir_skills_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    local_root = tmp_path / "local-skills"
    user_root = tmp_path / "user-skills"
    skill_dir = local_root / "installed" / "example"
    skill_dir.mkdir(parents=True)
    user_root.mkdir()
    (local_root / "skills-lock.json").write_text(
        json.dumps({"skills": {"example": {"path": "installed/example"}}}),
        encoding="utf-8",
    )
    (user_root / "skills-lock.json").write_text(json.dumps({"skills": {}}), encoding="utf-8")

    monkeypatch.setattr(skills_engine, "local_skills_dir", lambda workspace: local_root)
    monkeypatch.setattr(skills_engine, "user_skills_dir", lambda: user_root)

    assert run_cmd._skills_mounts_for_agent("pi", tmp_path) == [
        (str(skill_dir.resolve()), "/config/.pi/agent/skills/example", "ro")
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


def test_run_agent_publishes_ports(tmp_path: Path) -> None:
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
    config_dir = tmp_path / "agents" / "codex"
    workspace.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)

    manager.run_agent(
        agent="codex",
        image="vibepod/codex:latest",
        workspace=workspace,
        config_dir=config_dir,
        config_mount_path="/config",
        env={},
        command=["codex", "login"],
        auto_remove=True,
        name=None,
        version="0.2.1",
        network="vibepod-network",
        ports={"1456/tcp": 1455},
    )

    run_kwargs = manager.client.containers.run_kwargs  # type: ignore[union-attr]
    assert run_kwargs is not None
    # published callback forwarder, still on the user-defined network
    assert run_kwargs["ports"] == {"1456/tcp": 1455}
    assert run_kwargs["network"] == "vibepod-network"


class _KeepIdApi:
    def __init__(self) -> None:
        self.host_config_kwargs: dict | None = None
        self.host_config: dict | None = None
        self.create_kwargs: dict | None = None
        self.started: str | None = None

    def create_host_config(self, **kwargs):
        self.host_config_kwargs = kwargs
        self.host_config = {"Binds": kwargs["binds"], "AutoRemove": kwargs["auto_remove"]}
        return self.host_config

    def create_container(self, **kwargs):
        self.create_kwargs = kwargs
        return {"Id": "agent123"}

    def start(self, container_id: str) -> None:
        self.started = container_id


class _KeepIdContainers:
    def __init__(self) -> None:
        self.run_called = False

    def run(self, **kwargs):
        del kwargs
        self.run_called = True
        return {"id": "agent"}

    def get(self, container_id: str):
        return {"id": container_id}


class _KeepIdClient:
    def __init__(self) -> None:
        self.api = _KeepIdApi()
        self.containers = _KeepIdContainers()


class _EngineClient:
    def __init__(self, info: dict, version: dict) -> None:
        self.info_calls = 0
        self.version_calls = 0
        self._info = info
        self._version = version

    def info(self) -> dict:
        self.info_calls += 1
        return self._info

    def version(self) -> dict:
        self.version_calls += 1
        return self._version


def test_run_agent_uses_low_level_api_for_podman_keep_id(tmp_path: Path) -> None:
    client = _KeepIdClient()
    manager = object.__new__(DockerManager)
    manager.client = client  # type: ignore[assignment]

    workspace = tmp_path / "workspace"
    config_dir = tmp_path / "agents" / "pi"
    workspace.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)

    container = manager.run_agent(
        agent="pi",
        image="vibepod/pi:latest",
        workspace=workspace,
        config_dir=config_dir,
        config_mount_path="/config",
        env={"USER_UID": "0"},
        command=["pi"],
        auto_remove=True,
        name="vibepod-pi-test",
        version="0.14.0",
        network="vibepod-network",
        ports={"1456/tcp": 1455},
        extra_volumes=[(str(config_dir / "extra"), "/extra", "ro")],
        platform="linux/amd64",
        user="1000:1000",
        entrypoint=["/entrypoint.sh"],
        userns_mode="keep-id",
    )

    assert container == {"id": "agent123"}
    assert client.containers.run_called is False
    assert client.api.started == "agent123"
    assert client.api.host_config_kwargs == {
        "binds": [
            f"{workspace}:/workspace:rw",
            f"{config_dir}:/config:rw",
            f"{config_dir / 'extra'}:/extra:ro",
        ],
        "auto_remove": True,
        "network_mode": "vibepod-network",
        "port_bindings": {"1456/tcp": 1455},
    }
    assert client.api.host_config is not None
    assert client.api.host_config["UsernsMode"] == "keep-id"
    assert client.api.create_kwargs is not None
    assert client.api.create_kwargs["image"] == "vibepod/pi:latest"
    assert client.api.create_kwargs["name"] == "vibepod-pi-test"
    assert client.api.create_kwargs["command"] == ["pi"]
    assert client.api.create_kwargs["labels"]["vibepod.agent"] == "pi"
    assert client.api.create_kwargs["environment"] == {"USER_UID": "0"}
    assert client.api.create_kwargs["working_dir"] == "/workspace"
    assert client.api.create_kwargs["ports"] == ["1456/tcp"]
    assert client.api.create_kwargs["platform"] == "linux/amd64"
    assert client.api.create_kwargs["user"] == "1000:1000"
    assert client.api.create_kwargs["entrypoint"] == ["/entrypoint.sh"]


def test_run_agent_is_rootless_podman_detects_podman_engine() -> None:
    client = _EngineClient(
        {"Rootless": True, "SecurityOptions": ["name=rootless"]},
        {"Components": [{"Name": "Podman Engine", "Version": "5.7.0"}]},
    )
    manager = object.__new__(DockerManager)
    manager.client = client  # type: ignore[assignment]

    assert manager.is_rootless_podman() is True
    assert manager.is_rootless_podman() is True
    assert client.info_calls == 1
    assert client.version_calls == 1


@pytest.mark.parametrize(
    ("info", "version"),
    [
        (
            {"Rootless": True, "SecurityOptions": ["name=rootless"]},
            {"Components": [{"Name": "Docker Engine"}]},
        ),
        (
            {"Rootless": False, "SecurityOptions": []},
            {"Components": [{"Name": "Podman Engine"}]},
        ),
    ],
)
def test_run_agent_is_rootless_podman_requires_rootless_podman_evidence(
    info: dict, version: dict
) -> None:
    client = _EngineClient(info, version)
    manager = object.__new__(DockerManager)
    manager.client = client  # type: ignore[assignment]

    assert manager.is_rootless_podman() is False


def test_run_agent_is_rootless_podman_treats_sdk_failures_as_false() -> None:
    class _MissingVersionClient:
        def info(self) -> dict:
            return {"Rootless": True, "SecurityOptions": ["name=rootless"]}

    manager = object.__new__(DockerManager)
    manager.client = _MissingVersionClient()  # type: ignore[assignment]

    assert manager.is_rootless_podman() is False


def test_is_codex_oauth_login_detection() -> None:
    assert run_cmd._is_codex_oauth_login("codex", ["login"]) is True
    # device-code and api-key flows don't use the localhost:1455 callback
    assert run_cmd._is_codex_oauth_login("codex", ["login", "--device-auth"]) is False
    assert run_cmd._is_codex_oauth_login("codex", ["login", "--device-auth=true"]) is False
    assert run_cmd._is_codex_oauth_login("codex", ["login", "--with-api-key"]) is False
    assert run_cmd._is_codex_oauth_login("codex", ["login", "--with-api-key=secret"]) is False
    # non-login codex invocations and other agents stay on the normal path
    assert run_cmd._is_codex_oauth_login("codex", []) is False
    assert run_cmd._is_codex_oauth_login("claude", ["login"]) is False


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
    """Minimal DockerManager stub that records pull_image and run_agent calls."""

    def __init__(self, *, rootless_podman: bool = False) -> None:
        self.pulled: list[str] = []
        self.rootless_podman = rootless_podman
        self.run_kwargs: dict | None = None
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

    def is_rootless_podman(self) -> bool:
        return self.rootless_podman

    def pull_image(self, image: str) -> None:
        self.pulled.append(image)

    def ensure_proxy(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        pass

    def run_agent(self, **kwargs) -> object:  # type: ignore[no-untyped-def]
        self.run_kwargs = kwargs
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


@pytest.mark.parametrize("agent", SUPPORTED_AGENTS)
def test_run_uses_keep_id_for_rootless_podman_agents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, agent: str
) -> None:
    stub = _StubDockerManager(rootless_podman=True)
    config = _make_config()
    config["agents"][agent] = {"env": {}, "init": []}
    monkeypatch.setattr(run_cmd, "get_config", lambda: config)
    monkeypatch.setattr(run_cmd, "DockerManager", lambda: stub)

    run_cmd.run(agent=agent, workspace=tmp_path, detach=True)

    assert stub.run_kwargs is not None
    env = stub.run_kwargs["env"]
    assert stub.run_kwargs["userns_mode"] == "keep-id"
    assert stub.run_kwargs["user"] is None
    assert env["USER_UID"] == "0"
    assert env["USER_GID"] == "0"


def test_run_preserves_host_user_for_non_podman_devstral(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub = _StubDockerManager(rootless_podman=False)
    config = _make_config()
    config["agents"]["devstral"] = {"env": {}, "init": []}
    monkeypatch.setattr(run_cmd, "get_config", lambda: config)
    monkeypatch.setattr(run_cmd, "DockerManager", lambda: stub)
    monkeypatch.setattr(run_cmd.os, "getuid", lambda: 1234, raising=False)
    monkeypatch.setattr(run_cmd.os, "getgid", lambda: 5678, raising=False)

    run_cmd.run(agent="devstral", workspace=tmp_path, detach=True)

    assert stub.run_kwargs is not None
    env = stub.run_kwargs["env"]
    assert stub.run_kwargs["userns_mode"] is None
    assert stub.run_kwargs["user"] == "1234:5678"
    assert env["USER_UID"] == "1234"
    assert env["USER_GID"] == "5678"


def test_cli_run_forwards_extra_args_to_agent_command(monkeypatch, tmp_path: Path) -> None:
    """Extra args after -- are appended to the agent command as-is."""
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

    result = CliRunner().invoke(
        app,
        [
            "run",
            "-d",
            "-w",
            str(tmp_path),
            "claude",
            "--",
            "--model",
            "sonnet",
            "hello world",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["command"] == ["claude", "--model", "sonnet", "hello world"]


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



# ---------------------------------------------------------------------------
# Directory permission tests
# ---------------------------------------------------------------------------


def _make_capturing_docker_manager():
    """Return a Docker manager stub that records run_agent kwargs."""
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
            return type(
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

    return _CapturingDockerManager, captured


def test_run_aborts_when_dir_not_allowed_and_non_interactive(
    monkeypatch, tmp_path: Path
) -> None:
    """Non-interactive stdin + disallowed dir → Exit(1) with no prompt."""
    monkeypatch.setattr(run_cmd, "is_dir_allowed", lambda p: False)
    monkeypatch.setattr(run_cmd, "is_protected_dir", lambda p: False)
    monkeypatch.setattr(run_cmd, "get_config", lambda: _make_config())
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    with pytest.raises(typer.Exit) as exc:
        run_cmd.run(agent="claude", workspace=tmp_path, detach=True)
    assert exc.value.exit_code == 1


def test_run_aborts_on_protected_dir(monkeypatch, tmp_path: Path) -> None:
    """Protected directory (home/root) → Exit(1) without prompt."""
    monkeypatch.setattr(run_cmd, "is_dir_allowed", lambda p: False)
    monkeypatch.setattr(run_cmd, "is_protected_dir", lambda p: True)
    monkeypatch.setattr(run_cmd, "get_config", lambda: _make_config())

    with pytest.raises(typer.Exit) as exc:
        run_cmd.run(agent="claude", workspace=tmp_path, detach=True)
    assert exc.value.exit_code == 1


def test_run_prompts_and_proceeds_when_user_accepts(monkeypatch, tmp_path: Path) -> None:
    """Interactive: user accepts the prompt → dir is added and run proceeds."""
    added: list[Path] = []
    monkeypatch.setattr(run_cmd, "is_dir_allowed", lambda p: False)
    monkeypatch.setattr(run_cmd, "is_protected_dir", lambda p: False)
    monkeypatch.setattr(run_cmd, "add_allowed_dir", lambda p: added.append(p))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    # Patch Confirm.ask to always return True (user presses Y)
    _confirm_yes = type("_C", (), {"ask": staticmethod(lambda *a, **kw: True)})()
    monkeypatch.setattr(run_cmd, "Confirm", _confirm_yes)
    _CapturingDockerManager, _ = _make_capturing_docker_manager()
    monkeypatch.setattr(run_cmd, "get_config", lambda: _make_config())
    monkeypatch.setattr(run_cmd, "DockerManager", _CapturingDockerManager)

    run_cmd.run(agent="claude", workspace=tmp_path, detach=True)

    assert len(added) == 1
    assert added[0] == tmp_path.resolve()


def test_run_aborts_when_user_declines_prompt(monkeypatch, tmp_path: Path) -> None:
    """Interactive: user declines the prompt → Exit(1) and dir NOT added."""
    added: list[Path] = []
    monkeypatch.setattr(run_cmd, "is_dir_allowed", lambda p: False)
    monkeypatch.setattr(run_cmd, "is_protected_dir", lambda p: False)
    monkeypatch.setattr(run_cmd, "add_allowed_dir", lambda p: added.append(p))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    # Patch Confirm.ask to return False (user presses N)
    _confirm_no = type("_C", (), {"ask": staticmethod(lambda *a, **kw: False)})()
    monkeypatch.setattr(run_cmd, "Confirm", _confirm_no)
    monkeypatch.setattr(run_cmd, "get_config", lambda: _make_config())

    with pytest.raises(typer.Exit) as exc:
        run_cmd.run(agent="claude", workspace=tmp_path, detach=True)

    assert exc.value.exit_code == 1
    assert added == []
