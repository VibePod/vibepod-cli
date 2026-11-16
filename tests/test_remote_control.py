"""`vp run codex --remote-control` and the Codex remote-control helpers."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from vibepod.commands import run as run_cmd
from vibepod.core import remote_control as rc

_ENABLED_OUTPUT = (
    b"Starting app-server daemon with remote control enabled...\n"
    b"This machine is available for remote control as vibepod-project.\n"
    b"Daemon used app-server:\n"
    b"  path: /config/.codex/packages/app-server-daemon/current/bin/codex\n"
    b"  version: 0.166.0\n"
)
_PAIR_OUTPUT = json.dumps(
    {
        "pairingCode": "pairing-code",
        "manualPairingCode": "ABCD-1234",
        "environmentId": "env_test",
        "expiresAt": 1_700_000_000,
    },
).encode()


@pytest.fixture(autouse=True)
def _allow_all_dirs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_cmd, "is_dir_allowed", lambda p: True)


@pytest.fixture
def workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path / "config"))
    # Fixed identity so the exec user is the same on every platform.
    monkeypatch.setattr(
        run_cmd,
        "_host_identity_env",
        lambda: {"USER_UID": "1234", "USER_GID": "5678"},
    )
    project = tmp_path / "My Project"
    project.mkdir()
    return project


class _ExecContainer:
    """Started container that answers exec_run from a script."""

    name = "vibepod-codex-test"
    id = "abc123"
    status = "running"
    labels: dict = {}

    def __init__(self, results: list[tuple[int, bytes]]) -> None:
        self.attrs: dict = {"NetworkSettings": {"Networks": {}}}
        self._results = list(results)
        self.exec_calls: list[tuple[list[str], dict]] = []
        self.stopped = False

    def reload(self) -> None:
        pass

    def logs(self, **kwargs) -> bytes:  # type: ignore[no-untyped-def]
        return b""

    def exec_run(self, cmd, **kwargs):  # type: ignore[no-untyped-def]
        self.exec_calls.append((list(cmd), kwargs))
        exit_code, output = self._results.pop(0)
        return SimpleNamespace(exit_code=exit_code, output=output)

    def stop(self, timeout: int = 10) -> None:
        self.stopped = True


class _Manager:
    def __init__(self, container: _ExecContainer) -> None:
        self.container = container
        self.run_kwargs: dict | None = None

    def ensure_network(self, name: str) -> None:
        pass

    def is_rootless_podman(self) -> bool:
        return False

    def pull_image(self, image: str, auto_clean: bool = False) -> None:
        pass

    def networks_with_running_containers(self) -> list[str]:
        return []

    def run_agent(self, **kwargs):  # type: ignore[no-untyped-def]
        self.run_kwargs = kwargs
        return self.container

    def attach_interactive(self, container, logger=None) -> bytes:  # type: ignore[no-untyped-def]
        return b""


def _config(**overrides) -> dict:  # type: ignore[no-untyped-def]
    config: dict = {
        "default_agent": "codex",
        "auto_pull": False,
        "auto_remove": True,
        "network": "vibepod-network",
        "agents": {"codex": {"env": {}, "init": []}},
        "proxy": {"enabled": False},
        "logging": {"enabled": False},
    }
    config.update(overrides)
    return config


def _flat(text: str) -> str:
    """Join Rich's wrapped console lines so substrings can be matched."""
    return " ".join(text.split())


def _install(monkeypatch: pytest.MonkeyPatch, manager: object, config: dict | None = None) -> None:
    monkeypatch.setattr(run_cmd, "get_config", lambda: config or _config())
    monkeypatch.setattr(run_cmd, "DockerManager", lambda: manager)


class _UnusedManager:
    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        pytest.fail(f"refused launches must not touch the engine (called {name})")


# ---------------------------------------------------------------------------
# vp run
# ---------------------------------------------------------------------------


def test_remote_control_enables_codex_remote_control_in_the_container(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    container = _ExecContainer([(0, _ENABLED_OUTPUT)])
    manager = _Manager(container)
    _install(monkeypatch, manager)

    run_cmd.run(agent="codex", workspace=workspace, detach=True, remote_control=True)

    assert manager.run_kwargs is not None
    assert manager.run_kwargs["hostname"] == "vibepod-my-project"
    # The bare command: remote control for this daemon only, nothing persisted.
    assert container.exec_calls == [(["codex", "remote-control"], {"user": "1234:5678"})]
    out = _flat(capsys.readouterr().out)
    assert "This machine is available for remote control as vibepod-project." in out
    assert "Pairing code" not in out
    assert not container.stopped


def test_pair_implies_remote_control_and_prints_the_code(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    container = _ExecContainer([(0, _ENABLED_OUTPUT), (0, _PAIR_OUTPUT)])
    manager = _Manager(container)
    _install(monkeypatch, manager)

    run_cmd.run(agent="codex", workspace=workspace, detach=True, pair=True)

    assert [cmd for cmd, _ in container.exec_calls] == [
        ["codex", "remote-control"],
        ["codex", "remote-control", "pair", "--json"],
    ]
    assert "Pairing code: ABCD-1234" in _flat(capsys.readouterr().out)


@pytest.mark.parametrize(
    ("argv", "expected_calls"),
    [
        (["run", "codex", "--remote-control"], 1),
        (["x", "--pair"], 2),
        (["codex", "--pair"], 2),
    ],
)
def test_cli_exposes_the_flags_on_run_and_agent_aliases(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    argv: list[str],
    expected_calls: int,
) -> None:
    from typer.testing import CliRunner

    from vibepod.cli import app

    container = _ExecContainer([(0, _ENABLED_OUTPUT), (0, _PAIR_OUTPUT)])
    _install(monkeypatch, _Manager(container))

    result = CliRunner().invoke(app, [*argv, "-w", str(workspace), "--detach"])

    assert result.exit_code == 0, result.output
    assert len(container.exec_calls) == expected_calls


def test_codex_without_remote_control_keeps_the_default_hostname(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
) -> None:
    container = _ExecContainer([])
    manager = _Manager(container)
    _install(monkeypatch, manager)

    run_cmd.run(agent="codex", workspace=workspace, detach=True)

    assert manager.run_kwargs is not None
    assert manager.run_kwargs["hostname"] is None
    assert container.exec_calls == []


def test_remote_control_failure_stops_the_container(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    container = _ExecContainer(
        [
            (
                1,
                b"Error: remote control requires ChatGPT authentication; "
                b"API key auth is not supported\n",
            ),
        ],
    )
    _install(monkeypatch, _Manager(container))

    with pytest.raises(typer.Exit) as exc:
        run_cmd.run(agent="codex", workspace=workspace, detach=True, remote_control=True)

    assert exc.value.exit_code == 1
    assert container.stopped
    out = _flat(capsys.readouterr().out)
    assert "requires ChatGPT authentication" in out
    assert "vp run codex login" in out


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        ("claude", "vp run claude -- --remote-control"),
        ("copilot", "vp run copilot -- --remote"),
        ("agy", "vp run agy -- --remote-control"),
        ("gemini", "Supported: codex"),
    ],
)
def test_remote_control_is_refused_for_other_agents(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    agent: str,
    expected: str,
) -> None:
    _install(monkeypatch, _UnusedManager())

    with pytest.raises(typer.Exit) as exc:
        run_cmd.run(agent=agent, workspace=workspace, remote_control=True)

    assert exc.value.exit_code == 1
    assert expected in _flat(capsys.readouterr().out)


@pytest.mark.parametrize(
    ("options", "config_overrides", "expected"),
    [
        ({"provider_names": ["lab"]}, {}, "--provider"),
        ({}, {"llm": {"enabled": True, "base_url": "http://host.docker.internal:11434"}}, "llm"),
        ({"acp": True}, {}, "--acp"),
    ],
)
def test_remote_control_is_refused_with_conflicting_options(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    options: dict,
    config_overrides: dict,
    expected: str,
) -> None:
    _install(monkeypatch, _UnusedManager(), _config(**config_overrides))

    with pytest.raises(typer.Exit) as exc:
        run_cmd.run(agent="codex", workspace=workspace, remote_control=True, **options)

    assert exc.value.exit_code == 1
    captured = capsys.readouterr()
    # ACP mode routes console output to stderr.
    assert expected in _flat(captured.out + captured.err)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("My Project_v2", "vibepod-my-project-v2"),
        ("vibepod-cli", "vibepod-vibepod-cli"),
        ("---", "vibepod"),
        ("Ünïcode", "vibepod-n-code"),
    ],
)
def test_container_hostname_is_a_valid_label(name: str, expected: str) -> None:
    assert rc.container_hostname(Path("/work") / name) == expected


def test_container_hostname_is_capped_at_63_characters() -> None:
    hostname = rc.container_hostname(Path("/work") / ("a-" * 60))
    assert len(hostname) <= 63
    assert not hostname.endswith("-")


def test_exec_user_matches_the_agent_user() -> None:
    assert rc.exec_user({"USER_UID": "1", "USER_GID": "2"}, rootless_podman=False) == "1:2"
    assert rc.exec_user({}, rootless_podman=False) == "1000:1000"
    assert rc.exec_user({"USER_UID": "0", "USER_GID": "0"}, rootless_podman=True) is None


def test_enable_reports_a_timeout_when_codex_stays_in_the_foreground() -> None:
    release = threading.Event()

    class _BlockingContainer:
        def exec_run(self, cmd, **kwargs):  # type: ignore[no-untyped-def]
            release.wait(5)
            return SimpleNamespace(exit_code=0, output=b"")

    try:
        status = rc.enable_codex_remote_control(_BlockingContainer(), user=None, timeout=0.05)
    finally:
        release.set()

    assert status.timed_out
    assert "daemon_auto_start" in status.message


def test_enable_reports_engine_errors_as_failures() -> None:
    class _BrokenContainer:
        def exec_run(self, cmd, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("container is not running")

    with pytest.raises(rc.RemoteControlError, match="container is not running"):
        rc.enable_codex_remote_control(_BrokenContainer(), user=None, timeout=5)


def test_pairing_parses_the_json_response() -> None:
    container = _ExecContainer([(0, b"warning: something\n" + _PAIR_OUTPUT + b"\n")])

    code = rc.start_codex_pairing(container, user="1:2")

    assert code.code == "ABCD-1234"
    assert code.expires_at is not None
    assert container.exec_calls == [
        (["codex", "remote-control", "pair", "--json"], {"user": "1:2"}),
    ]


@pytest.mark.parametrize(
    ("exit_code", "output", "match"),
    [
        (1, b"Error: no daemon is running\n", "no daemon is running"),
        (0, b'{"pairingCode": "x"}\n', "no pairing code"),
        (0, b"not json\n", "no pairing code"),
    ],
)
def test_pairing_failures_raise(exit_code: int, output: bytes, match: str) -> None:
    container = _ExecContainer([(exit_code, output)])

    with pytest.raises(rc.RemoteControlError, match=match):
        rc.start_codex_pairing(container, user=None)


def test_failure_hints() -> None:
    assert "vp run codex login" in (
        rc.remote_control_failure_hint("remote control requires ChatGPT authentication") or ""
    )
    assert "--pull" in (
        rc.remote_control_failure_hint("error: unrecognized subcommand 'remote-control'") or ""
    )
    assert rc.remote_control_failure_hint("something else") is None
