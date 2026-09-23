"""Provider selection forwarding and early failure checks."""

import os

import pytest
from typer.testing import CliRunner

from vibepod.cli import app
from vibepod.commands import run

# The provider store enforces POSIX owner-only permissions and refuses other platforms.
pytestmark = pytest.mark.skipif(os.name == "nt", reason="provider store is POSIX-only")

runner = CliRunner()


@pytest.mark.parametrize(
    "agent,protocol",
    [("claude", "anthropic"), ("pi", "openai-chat"), ("codex", "openai-responses")],
)
@pytest.mark.parametrize("with_init", [False, True])
def test_temporary_provider_reaches_container_and_legacy_is_preserved(
    monkeypatch,
    tmp_path,
    agent,
    protocol,
    with_init,
):
    from unittest.mock import MagicMock

    from vibepod.core.config import _default_config
    from vibepod.core.providers import Provider, save_provider

    monkeypatch.setenv("VP_PROVIDERS_DIR", str(tmp_path / "providers"))
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path / "config"))
    save_provider(
        Provider(
            "hosted",
            protocol,
            "https://example.com",
            auth="key",
            models=("m",),
            default_model="m",
        ),
        key="secret",
    )
    config = _default_config()
    config["proxy"]["enabled"] = False
    if with_init:
        config["agents"][agent]["init"] = ["echo initialization"]
    config["llm"] = {
        "enabled": True,
        "base_url": "https://old.example",
        "api_key": "old",
        "model": "old",
    }
    manager = MagicMock()
    manager.resolve_launch_command.side_effect = lambda *, image, command: [
        "native-entrypoint",
        *command,
    ]
    manager.is_rootless_podman.return_value = False
    manager.supports_host_socket_mounts.return_value = False
    manager.networks_with_running_containers.return_value = []
    container = manager.run_agent.return_value
    container.name = "test-container"
    container.id = "123"
    container.status = "running"
    container.attrs = {"NetworkSettings": {"Networks": {}}}
    monkeypatch.setattr(run, "DockerManager", lambda: manager)
    monkeypatch.setattr(run, "get_config", lambda: config)
    monkeypatch.setattr(run, "is_dir_allowed", lambda _: True)
    monkeypatch.setattr(run, "_reexec_with_herdr_hint", lambda *a, **k: None)
    run.run(
        agent=agent,
        workspace=tmp_path,
        detach=True,
        no_herdr=True,
        no_overlay=True,
        provider_names=["hosted"],
    )
    kwargs = manager.run_agent.call_args.kwargs
    assert kwargs["extra_labels"]["vibepod.provider"] == "hosted"
    if agent == "claude":
        assert kwargs["env"]["ANTHROPIC_API_KEY"] == "secret"
        assert kwargs["env"]["ANTHROPIC_BASE_URL"] == "https://example.com"
        assert kwargs["command"] == (["native-entrypoint"] if with_init else []) + [
            "claude",
            "--model",
            "m",
        ]
    else:
        import json

        from vibepod.core.provider_runtime import BOOTSTRAP_MOUNT, COMMAND_ENV

        assert kwargs["env"]["VIBEPOD_PROVIDER_KEY_0"] == "secret"
        prefix = ["native-entrypoint"] if with_init else []
        assert kwargs["command"] == [*prefix, "node", BOOTSTRAP_MOUNT]
        wrapped = json.loads(kwargs["env"][COMMAND_ENV])
        assert wrapped[0] == agent
        assert wrapped[-2:] == ["--model", "m"]
        assert "secret" not in kwargs["env"][COMMAND_ENV]
        mounts = [v for v in kwargs["extra_volumes"] if v[1] == BOOTSTRAP_MOUNT]
        assert len(mounts) == 1 and mounts[0][2] == "ro"
        assert mounts[0][0].startswith(str(tmp_path / "config"))
    assert config["llm"]["api_key"] == "old"
    run.run(agent="claude", workspace=tmp_path, detach=True, no_herdr=True, no_overlay=True)
    assert manager.run_agent.call_args.kwargs["env"]["ANTHROPIC_API_KEY"] == "old"
    assert "vibepod.provider" not in manager.run_agent.call_args.kwargs["extra_labels"]


@pytest.mark.parametrize("override", [["--model", "other"], ["--model=other"], ["-m", "other"]])
def test_explicit_model_passthrough_wins_over_provider_default(monkeypatch, tmp_path, override):
    from unittest.mock import MagicMock

    from vibepod.core.config import _default_config
    from vibepod.core.providers import Provider, save_provider

    monkeypatch.setenv("VP_PROVIDERS_DIR", str(tmp_path / "providers"))
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path / "config"))
    save_provider(
        Provider("hosted", "anthropic", "https://example.com", models=("m",), default_model="m"),
    )
    config = _default_config()
    config["proxy"]["enabled"] = False
    manager = MagicMock()
    manager.is_rootless_podman.return_value = False
    manager.supports_host_socket_mounts.return_value = False
    manager.networks_with_running_containers.return_value = []
    container = manager.run_agent.return_value
    container.name = "test-container"
    container.id = "123"
    container.status = "running"
    container.attrs = {"NetworkSettings": {"Networks": {}}}
    monkeypatch.setattr(run, "DockerManager", lambda: manager)
    monkeypatch.setattr(run, "get_config", lambda: config)
    monkeypatch.setattr(run, "is_dir_allowed", lambda _: True)
    monkeypatch.setattr(run, "_reexec_with_herdr_hint", lambda *a, **k: None)
    run.run(
        agent="claude",
        workspace=tmp_path,
        detach=True,
        no_herdr=True,
        no_overlay=True,
        provider_names=["hosted"],
        passthrough_args=override,
    )
    kwargs = manager.run_agent.call_args.kwargs
    assert kwargs["command"] == ["claude", *override]
    assert kwargs["env"]["ANTHROPIC_MODEL"] == "m"


def test_provider_flag_forwarded_on_run_and_shortcut(monkeypatch):
    calls = []
    monkeypatch.setattr(run, "run", lambda **kwargs: calls.append(kwargs))
    for command in (["run", "claude"], ["c"]):
        result = runner.invoke(app, [*command, "--provider", "hosted"])
        assert result.exit_code == 0, result.output
        assert calls[-1]["provider_names"] == ["hosted"]
        assert calls[-1]["passthrough_args"] == []


def test_unsupported_provider_agent_fails_before_docker(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "get_config", lambda: {})
    monkeypatch.setattr(run, "_reexec_with_herdr_hint", lambda *args, **kwargs: None)

    def no_docker():
        raise AssertionError("Docker must not be contacted")

    monkeypatch.setattr(run, "DockerManager", no_docker)
    result = runner.invoke(app, ["run", "gemini", "--provider", "hosted", "-w", str(tmp_path)])
    assert result.exit_code == 1
    assert "not yet supported" in result.output


@pytest.mark.parametrize("agent", ["opencode", "tau", "jcode", "qwen"])
def test_new_agents_provider_reaches_container(monkeypatch, tmp_path, agent):
    import json as _json
    from unittest.mock import MagicMock

    from vibepod.core.config import _default_config
    from vibepod.core.provider_runtime import (
        BOOTSTRAP_MOUNT,
        BOOTSTRAP_MOUNT_PY,
        BOOTSTRAP_MOUNT_SH,
        COMMAND_B64_ENV,
        COMMAND_ENV,
    )
    from vibepod.core.providers import Provider, save_provider

    monkeypatch.setenv("VP_PROVIDERS_DIR", str(tmp_path / "providers"))
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path / "config"))
    save_provider(
        Provider(
            "hosted",
            "openai-chat",
            "https://example.com/v1",
            auth="key",
            models=("m",),
            default_model="m",
        ),
        key="secret",
    )
    config = _default_config()
    config["proxy"]["enabled"] = False
    config["llm"] = {
        "enabled": True,
        "base_url": "https://old.example",
        "api_key": "old",
        "model": "old",
    }
    manager = MagicMock()
    manager.resolve_launch_command.side_effect = lambda *, image, command: [
        "native-entrypoint",
        *command,
    ]
    manager.is_rootless_podman.return_value = False
    manager.supports_host_socket_mounts.return_value = False
    manager.networks_with_running_containers.return_value = []
    container = manager.run_agent.return_value
    container.name = "test-container"
    container.id = "123"
    container.status = "running"
    container.attrs = {"NetworkSettings": {"Networks": {}}}
    monkeypatch.setattr(run, "DockerManager", lambda: manager)
    monkeypatch.setattr(run, "get_config", lambda: config)
    monkeypatch.setattr(run, "is_dir_allowed", lambda _: True)
    monkeypatch.setattr(run, "_reexec_with_herdr_hint", lambda *a, **k: None)
    run.run(
        agent=agent,
        workspace=tmp_path,
        detach=True,
        no_herdr=True,
        no_overlay=True,
        provider_names=["hosted"],
    )
    kwargs = manager.run_agent.call_args.kwargs
    assert kwargs["extra_labels"]["vibepod.provider"] == "hosted"
    assert "old" not in kwargs["env"].values()
    if agent == "qwen":
        assert kwargs["env"]["OPENAI_API_KEY"] == "secret"
        assert kwargs["env"]["OPENAI_BASE_URL"] == "https://example.com/v1"
        assert kwargs["env"]["OPENAI_MODEL"] == "m"
        # Env-only adapter: the startup model travels in OPENAI_MODEL, not argv.
        assert kwargs["command"] == ["qwen"]
    else:
        assert kwargs["env"]["VIBEPOD_PROVIDER_KEY_0"] == "secret"
        if agent == "opencode":
            assert kwargs["command"] == ["node", BOOTSTRAP_MOUNT]
        elif agent == "tau":
            assert kwargs["command"] == ["python3", BOOTSTRAP_MOUNT_PY]
        else:
            assert kwargs["command"] == ["sh", BOOTSTRAP_MOUNT_SH]
        if agent == "jcode":
            import base64

            words = kwargs["env"][COMMAND_B64_ENV].split(" ")
            wrapped = [base64.b64decode(w[1:]).decode() for w in words]
            assert "secret" not in kwargs["env"][COMMAND_B64_ENV]
            assert kwargs["env"]["VIBEPOD_PROVIDER_NAMES"] == "hosted"
            assert "[providers.hosted]" in kwargs["env"]["VIBEPOD_PROVIDER_CONFIG_TOML"]
        else:
            wrapped = _json.loads(kwargs["env"][COMMAND_ENV])
            assert "secret" not in kwargs["env"][COMMAND_ENV]
        assert wrapped[0] == agent
        mounts = [v for v in kwargs["extra_volumes"] if "/provider-bootstrap." in v[1]]
        assert len(mounts) == 1 and mounts[0][2] == "ro"
        assert mounts[0][0].startswith(str(tmp_path / "config"))


def test_acp_provider_rejected_for_all_wrapped_agents(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    from vibepod.core.config import _default_config
    from vibepod.core.providers import Provider, save_provider

    monkeypatch.setenv("VP_PROVIDERS_DIR", str(tmp_path / "providers"))
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path / "config"))
    save_provider(Provider("hosted", "openai-chat", "https://example.com/v1", models=("m",)))
    config = _default_config()
    config["proxy"]["enabled"] = False
    manager = MagicMock()
    monkeypatch.setattr(run, "DockerManager", lambda: manager)
    monkeypatch.setattr(run, "get_config", lambda: config)
    monkeypatch.setattr(run, "is_dir_allowed", lambda _: True)
    monkeypatch.setattr(run, "_reexec_with_herdr_hint", lambda *a, **k: None)
    for agent in ("pi", "codex", "opencode", "tau", "jcode"):
        result = runner.invoke(
            app,
            ["run", agent, "--provider", "hosted", "--acp", "-w", str(tmp_path)],
        )
        assert result.exit_code == 1, result.output
        assert "not yet supported in ACP mode" in result.output
    assert not manager.run_agent.called
