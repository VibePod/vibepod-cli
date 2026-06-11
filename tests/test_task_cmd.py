"""Task command tests."""

from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from vibepod.cli import app
from vibepod.commands import task as task_cmd
from vibepod.core.agents import AGENT_SPECS
from vibepod.core.tasks import TaskStore

# ---------------------------------------------------------------------------
# Autouse: allow workspace dirs so prompts don't block
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _allow_all_dirs(monkeypatch):
    monkeypatch.setattr(task_cmd, "is_dir_allowed", lambda p: True)
    monkeypatch.setattr(
        task_cmd,
        "_start_timeout_watcher",
        lambda task_id, timeout_seconds: None,
    )


@pytest.fixture
def tmp_task_store(tmp_path, monkeypatch):
    """Redirect the task store to a fresh tmp path."""
    db_path = tmp_path / "tasks.db"
    monkeypatch.setattr(task_cmd, "_task_store", lambda: TaskStore(db_path))
    return TaskStore(db_path)


@pytest.fixture
def fake_ctx():
    """Minimal ctx stand-in for direct task_run() calls (no passthrough args)."""
    import types

    return types.SimpleNamespace(args=[])


def _make_config() -> dict:
    return {
        "default_agent": "claude",
        "auto_pull": False,
        "auto_remove": True,
        "network": "vibepod-network",
        "agents": {
            "claude": {"env": {}, "init": []},
            "codex": {"env": {}, "init": []},
            "auggie": {"env": {}, "init": []},
            "gemini": {"env": {}, "init": []},
        },
        "proxy": {"enabled": False},
        "logging": {"enabled": False},
    }


class _CapturingDockerManager:
    """Docker manager stub that records run_agent kwargs."""

    def __init__(self) -> None:
        self.run_kwargs: dict | None = None

    def ensure_network(self, name: str) -> None:
        pass

    def networks_with_running_containers(self) -> list[str]:
        return []

    def pull_image(self, image: str) -> None:
        pass

    def ensure_proxy(self, **kwargs) -> None:
        pass

    def resolve_launch_command(self, image: str, command: list[str] | None) -> list[str]:
        # Tests that exercise init commands or a None command can mock this;
        # the happy-path tests never hit it.
        return command or []

    def run_agent(self, **kwargs):
        self.run_kwargs = kwargs
        return type(
            "_Container",
            (),
            {
                "id": "cid123456789012",
                "name": kwargs.get("name") or "vibepod-task-abcdef",
                "status": "running",
                "attrs": {"NetworkSettings": {"Networks": {}}},
                "reload": lambda self: None,
                "labels": {},
                "logs": lambda self, **kw: b"",
            },
        )()


# ---------------------------------------------------------------------------
# AgentSpec.headless_prefix wiring
# ---------------------------------------------------------------------------


def test_headless_prefix_set_for_supported_agents() -> None:
    assert AGENT_SPECS["claude"].headless_prefix == ["-p"]
    assert AGENT_SPECS["codex"].headless_prefix == ["exec"]
    assert AGENT_SPECS["auggie"].headless_prefix == ["--print"]


def test_headless_prefix_none_for_unsupported_agents() -> None:
    assert AGENT_SPECS["gemini"].headless_prefix is None
    assert AGENT_SPECS["opencode"].headless_prefix is None
    assert AGENT_SPECS["devstral"].headless_prefix is None
    assert AGENT_SPECS["copilot"].headless_prefix is None


# ---------------------------------------------------------------------------
# task create — agent validation
# ---------------------------------------------------------------------------


def test_task_create_rejects_unknown_agent(monkeypatch, tmp_path, tmp_task_store) -> None:
    monkeypatch.setattr(task_cmd, "get_config", _make_config)

    with pytest.raises(typer.Exit) as exc:
        task_cmd.task_create(agent="nosuchthing", prompt="hi", workspace=tmp_path)
    assert exc.value.exit_code == 1


def test_task_create_rejects_agent_without_headless_prefix(
    monkeypatch, tmp_path, tmp_task_store
) -> None:
    monkeypatch.setattr(task_cmd, "get_config", _make_config)

    with pytest.raises(typer.Exit) as exc:
        task_cmd.task_create(agent="gemini", prompt="hi", workspace=tmp_path)
    assert exc.value.exit_code == 1


# ---------------------------------------------------------------------------
# task create — happy path, command shape
# ---------------------------------------------------------------------------


def test_task_create_claude_builds_headless_command(monkeypatch, tmp_path, tmp_task_store) -> None:
    stub = _CapturingDockerManager()
    monkeypatch.setattr(task_cmd, "get_config", _make_config)
    monkeypatch.setattr(task_cmd, "DockerManager", lambda: stub)

    task_cmd.task_create(agent="claude", prompt="do the thing", workspace=tmp_path)

    assert stub.run_kwargs is not None
    assert stub.run_kwargs["command"] == ["claude", "-p", "do the thing"]
    assert stub.run_kwargs["auto_remove"] is False
    assert stub.run_kwargs["agent"] == "claude"


def test_task_create_codex_uses_exec_subcommand(monkeypatch, tmp_path, tmp_task_store) -> None:
    stub = _CapturingDockerManager()
    monkeypatch.setattr(task_cmd, "get_config", _make_config)
    monkeypatch.setattr(task_cmd, "DockerManager", lambda: stub)

    task_cmd.task_create(agent="codex", prompt="refactor auth", workspace=tmp_path)

    assert stub.run_kwargs["command"] == ["codex", "exec", "refactor auth"]


def test_task_create_codex_aliases_openai_api_key(monkeypatch, tmp_path, tmp_task_store) -> None:
    stub = _CapturingDockerManager()
    cfg = _make_config()
    cfg["agents"]["codex"] = {
        "env": {"OPENAI_API_KEY": "sk-test-key"},
        "init": [],
    }
    monkeypatch.setattr(task_cmd, "get_config", lambda: cfg)
    monkeypatch.setattr(task_cmd, "DockerManager", lambda: stub)

    task_cmd.task_create(agent="codex", prompt="say ok", workspace=tmp_path)

    env = stub.run_kwargs["env"]
    assert env["OPENAI_API_KEY"] == "sk-test-key"
    assert env["CODEX_API_KEY"] == "sk-test-key"


def test_task_create_auggie_uses_print_flag(monkeypatch, tmp_path, tmp_task_store) -> None:
    stub = _CapturingDockerManager()
    monkeypatch.setattr(task_cmd, "get_config", _make_config)
    monkeypatch.setattr(task_cmd, "DockerManager", lambda: stub)

    task_cmd.task_create(agent="auggie", prompt="run tests", workspace=tmp_path)

    assert stub.run_kwargs["command"] == ["auggie", "--print", "run tests"]


def test_task_create_ikwid_appends_ikwid_args_before_headless_prefix(
    monkeypatch, tmp_path, tmp_task_store
) -> None:
    stub = _CapturingDockerManager()
    monkeypatch.setattr(task_cmd, "get_config", _make_config)
    monkeypatch.setattr(task_cmd, "DockerManager", lambda: stub)

    task_cmd.task_create(agent="claude", prompt="do it", workspace=tmp_path, ikwid=True)

    assert stub.run_kwargs["command"] == [
        "claude",
        "--dangerously-skip-permissions",
        "-p",
        "do it",
    ]


def test_task_create_passthrough_args_appended_after_prompt(
    monkeypatch, tmp_path, tmp_task_store
) -> None:
    """Extra args after `--` are appended to the agent's command after the prompt."""
    stub = _CapturingDockerManager()
    monkeypatch.setattr(task_cmd, "get_config", _make_config)
    monkeypatch.setattr(task_cmd, "DockerManager", lambda: stub)

    result = CliRunner().invoke(
        app,
        [
            "task",
            "create",
            "-w",
            str(tmp_path),
            "claude",
            "summarize",
            "--",
            "--output-format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert stub.run_kwargs["command"] == [
        "claude",
        "-p",
        "summarize",
        "--output-format",
        "json",
    ]


def test_task_run_alias_still_starts_task(monkeypatch, tmp_path, tmp_task_store) -> None:
    stub = _CapturingDockerManager()
    monkeypatch.setattr(task_cmd, "get_config", _make_config)
    monkeypatch.setattr(task_cmd, "DockerManager", lambda: stub)

    result = CliRunner().invoke(
        app,
        ["task", "run", "-w", str(tmp_path), "claude", "summarize"],
    )

    assert result.exit_code == 0, result.output
    assert "deprecated" in result.output
    assert stub.run_kwargs is not None
    assert stub.run_kwargs["command"] == ["claude", "-p", "summarize"]


def test_task_create_records_task_in_store(monkeypatch, tmp_path, tmp_task_store) -> None:
    stub = _CapturingDockerManager()
    monkeypatch.setattr(task_cmd, "get_config", _make_config)
    monkeypatch.setattr(task_cmd, "DockerManager", lambda: stub)

    task_cmd.task_create(agent="claude", prompt="do a thing", workspace=tmp_path)

    rows = tmp_task_store.list()
    assert len(rows) == 1
    assert rows[0].agent == "claude"
    assert rows[0].prompt == "do a thing"
    assert rows[0].container_id == "cid123456789012"
    assert rows[0].status == "running"


def test_task_create_starts_default_timeout_watcher(monkeypatch, tmp_path, tmp_task_store) -> None:
    stub = _CapturingDockerManager()
    launched: list[tuple[str, int]] = []
    monkeypatch.setattr(task_cmd, "get_config", _make_config)
    monkeypatch.setattr(task_cmd, "DockerManager", lambda: stub)
    monkeypatch.setattr(
        task_cmd,
        "_start_timeout_watcher",
        lambda task_id, timeout_seconds: launched.append((task_id, timeout_seconds)),
    )

    task_cmd.task_create(agent="claude", prompt="do a thing", workspace=tmp_path)

    rows = tmp_task_store.list()
    assert launched == [(rows[0].id, 7200)]


def test_task_create_accepts_timeout_override(monkeypatch, tmp_path, tmp_task_store) -> None:
    stub = _CapturingDockerManager()
    launched: list[tuple[str, int]] = []
    monkeypatch.setattr(task_cmd, "get_config", _make_config)
    monkeypatch.setattr(task_cmd, "DockerManager", lambda: stub)
    monkeypatch.setattr(
        task_cmd,
        "_start_timeout_watcher",
        lambda task_id, timeout_seconds: launched.append((task_id, timeout_seconds)),
    )

    task_cmd.task_create(
        agent="claude",
        prompt="do a thing",
        workspace=tmp_path,
        timeout="30m",
    )

    rows = tmp_task_store.list()
    assert launched == [(rows[0].id, 1800)]


def test_task_create_timeout_none_disables_watcher(monkeypatch, tmp_path, tmp_task_store) -> None:
    stub = _CapturingDockerManager()
    monkeypatch.setattr(task_cmd, "get_config", _make_config)
    monkeypatch.setattr(task_cmd, "DockerManager", lambda: stub)
    monkeypatch.setattr(
        task_cmd,
        "_start_timeout_watcher",
        lambda task_id, timeout_seconds: pytest.fail("watcher should not start"),
    )

    task_cmd.task_create(
        agent="claude",
        prompt="do a thing",
        workspace=tmp_path,
        timeout="none",
    )


def test_timeout_watcher_stops_running_container_and_marks_failed(
    monkeypatch, tmp_task_store
) -> None:
    record = tmp_task_store.create(
        agent="claude",
        prompt="run forever",
        workspace="/ws",
        container_id="running",
        container_name="vibepod-task-running",
        image="vibepod/claude:latest",
        vibepod_version="0.11.0",
    )

    class _RunningContainer:
        status = "running"
        attrs = {"State": {"Status": "running"}}

        def __init__(self) -> None:
            self.stop_timeout: int | None = None

        def reload(self) -> None:
            pass

        def stop(self, timeout: int = 10) -> None:
            self.stop_timeout = timeout

    container = _RunningContainer()

    class _Manager:
        def get_container(self, name_or_id: str):
            assert name_or_id == "running"
            return container

    monkeypatch.setattr(task_cmd, "DockerManager", lambda: _Manager())

    task_cmd._enforce_task_timeout(record.id, 30, sleep=lambda seconds: None)

    updated = tmp_task_store.get(record.id)
    assert container.stop_timeout == 10
    assert updated is not None
    assert updated.status == "failed"
    assert updated.finished_at is not None


# ---------------------------------------------------------------------------
# task list / logs / status / rm
# ---------------------------------------------------------------------------


def test_task_list_shows_recorded_tasks(monkeypatch, tmp_task_store) -> None:
    tmp_task_store.create(
        agent="claude",
        prompt="do a",
        workspace="/ws",
        container_id="aaaaaa",
        container_name="vibepod-task-a",
        image="vibepod/claude:latest",
        vibepod_version="0.11.0",
    )
    # Make DockerManager unavailable so list uses persisted task state (no network calls).
    from vibepod.core.docker import DockerClientError

    class _Unavailable:
        def __init__(self) -> None:
            raise DockerClientError("no docker")

    monkeypatch.setattr(task_cmd, "DockerManager", _Unavailable)

    result = CliRunner().invoke(app, ["task", "list", "--json"])
    assert result.exit_code == 0, result.output
    assert '"agent": "claude"' in result.output
    assert '"prompt": "do a"' in result.output


def test_task_status_persists_terminal_container_state(monkeypatch, tmp_task_store) -> None:
    record = tmp_task_store.create(
        agent="claude",
        prompt="done",
        workspace="/ws",
        container_id="exited",
        container_name="vibepod-task-exited",
        image="vibepod/claude:latest",
        vibepod_version="0.11.0",
    )

    class _ExitedContainer:
        attrs = {
            "State": {
                "Status": "exited",
                "ExitCode": 0,
                "StartedAt": "2026-06-11T14:00:00Z",
                "FinishedAt": "2026-06-11T14:05:00Z",
            }
        }

        def reload(self) -> None:
            pass

    class _Manager:
        def get_container(self, name_or_id: str):
            assert name_or_id == "exited"
            return _ExitedContainer()

    monkeypatch.setattr(task_cmd, "DockerManager", lambda: _Manager())

    result = CliRunner().invoke(app, ["task", "status", record.id, "--json"])

    assert result.exit_code == 0, result.output
    updated = tmp_task_store.get(record.id)
    assert updated is not None
    assert updated.status == "completed"
    assert updated.exit_code == 0
    assert updated.started_at == "2026-06-11T14:00:00Z"
    assert updated.finished_at == "2026-06-11T14:05:00Z"
    assert '"status": "completed"' in result.output


def test_task_status_uses_persisted_terminal_state_when_container_removed(
    monkeypatch, tmp_task_store
) -> None:
    record = tmp_task_store.create(
        agent="claude",
        prompt="done",
        workspace="/ws",
        container_id="gone",
        container_name="vibepod-task-gone",
        image="vibepod/claude:latest",
        vibepod_version="0.11.0",
    )
    tmp_task_store.update(
        record.id,
        status="failed",
        exit_code=2,
        started_at="2026-06-11T14:00:00Z",
        finished_at="2026-06-11T14:05:00Z",
    )

    class _Manager:
        def get_container(self, name_or_id: str):
            from vibepod.core.docker import DockerClientError

            raise DockerClientError("gone")

    monkeypatch.setattr(task_cmd, "DockerManager", lambda: _Manager())

    result = CliRunner().invoke(app, ["task", "status", record.id[:12], "--json"])

    assert result.exit_code == 0, result.output
    assert '"status": "failed"' in result.output
    assert '"exit_code": 2' in result.output


def test_task_list_persists_terminal_container_state(monkeypatch, tmp_task_store) -> None:
    record = tmp_task_store.create(
        agent="claude",
        prompt="done",
        workspace="/ws",
        container_id="exited",
        container_name="vibepod-task-exited",
        image="vibepod/claude:latest",
        vibepod_version="0.11.0",
    )

    class _ExitedContainer:
        attrs = {"State": {"Status": "exited", "ExitCode": 1}}

        def reload(self) -> None:
            pass

    class _Manager:
        def get_container(self, name_or_id: str):
            assert name_or_id == "exited"
            return _ExitedContainer()

    monkeypatch.setattr(task_cmd, "DockerManager", lambda: _Manager())

    result = CliRunner().invoke(app, ["task", "list", "--json"])

    assert result.exit_code == 0, result.output
    updated = tmp_task_store.get(record.id)
    assert updated is not None
    assert updated.status == "failed"
    assert updated.exit_code == 1
    assert '"status": "failed"' in result.output


def test_task_list_uses_removed_status_when_container_removed(monkeypatch, tmp_task_store) -> None:
    tmp_task_store.create(
        agent="claude",
        prompt="removed task",
        workspace="/ws",
        container_id="gone",
        container_name="vibepod-task-gone",
        image="vibepod/claude:latest",
        vibepod_version="0.11.0",
    )

    class _Manager:
        def get_container(self, name_or_id: str):
            from vibepod.core.docker import DockerClientError

            raise DockerClientError("gone")

    monkeypatch.setattr(task_cmd, "DockerManager", lambda: _Manager())

    result = CliRunner().invoke(app, ["task", "list", "--json"])

    assert result.exit_code == 0, result.output
    assert '"status": "removed"' in result.output


def test_task_rm_deletes_store_row_when_container_gone(monkeypatch, tmp_task_store) -> None:
    record = tmp_task_store.create(
        agent="claude",
        prompt="gone",
        workspace="/ws",
        container_id="zzzz",
        container_name="vibepod-task-z",
        image="vibepod/claude:latest",
        vibepod_version="0.11.0",
    )

    class _ManagerContainerMissing:
        def get_container(self, name_or_id: str):
            from vibepod.core.docker import DockerClientError

            raise DockerClientError(f"Container '{name_or_id}' not found")

    monkeypatch.setattr(task_cmd, "DockerManager", lambda: _ManagerContainerMissing())

    task_cmd.task_rm(task_id=record.id[:10], force=False)

    assert tmp_task_store.get(record.id) is None


def test_task_rm_refuses_running_container_without_force(monkeypatch, tmp_task_store) -> None:
    record = tmp_task_store.create(
        agent="claude",
        prompt="still going",
        workspace="/ws",
        container_id="rrr",
        container_name="vibepod-task-r",
        image="vibepod/claude:latest",
        vibepod_version="0.11.0",
    )

    class _FakeContainer:
        status = "running"

        def reload(self) -> None:
            pass

        def remove(self, force: bool = False) -> None:  # pragma: no cover
            raise AssertionError("should not remove without --force")

    class _Manager:
        def get_container(self, name_or_id: str):
            return _FakeContainer()

    monkeypatch.setattr(task_cmd, "DockerManager", lambda: _Manager())

    with pytest.raises(typer.Exit) as exc:
        task_cmd.task_rm(task_id=record.id, force=False)
    assert exc.value.exit_code == 1
    # Row is NOT deleted because rm was refused.
    assert tmp_task_store.get(record.id) is not None


def test_task_rm_requires_id_or_all(tmp_task_store) -> None:
    with pytest.raises(typer.BadParameter):
        task_cmd.task_rm(task_id=None, all_tasks=False, force=False)


def test_task_rm_all_rejects_task_id(tmp_task_store) -> None:
    with pytest.raises(typer.BadParameter):
        task_cmd.task_rm(task_id="abc123", all_tasks=True, force=False)


def test_task_rm_all_refuses_running_containers_without_force(monkeypatch, tmp_task_store) -> None:
    running = tmp_task_store.create(
        agent="claude",
        prompt="still going",
        workspace="/ws",
        container_id="running",
        container_name="vibepod-task-running",
        image="vibepod/claude:latest",
        vibepod_version="0.11.0",
    )
    stopped = tmp_task_store.create(
        agent="codex",
        prompt="done",
        workspace="/ws",
        container_id="stopped",
        container_name="vibepod-task-stopped",
        image="vibepod/codex:latest",
        vibepod_version="0.11.0",
    )

    class _FakeContainer:
        def __init__(self, status: str) -> None:
            self.status = status
            self.removed = False

        def reload(self) -> None:
            pass

        def remove(self, force: bool = False) -> None:  # pragma: no cover
            self.removed = True

    containers = {
        "running": _FakeContainer("running"),
        "stopped": _FakeContainer("exited"),
    }

    class _Manager:
        def get_container(self, name_or_id: str):
            return containers[name_or_id]

    monkeypatch.setattr(task_cmd, "DockerManager", lambda: _Manager())

    with pytest.raises(typer.Exit) as exc:
        task_cmd.task_rm(task_id=None, all_tasks=True, force=False)

    assert exc.value.exit_code == 1
    assert tmp_task_store.get(running.id) is not None
    assert tmp_task_store.get(stopped.id) is not None
    assert not containers["running"].removed
    assert not containers["stopped"].removed


def test_task_rm_all_force_removes_all_records_and_containers(monkeypatch, tmp_task_store) -> None:
    running = tmp_task_store.create(
        agent="claude",
        prompt="still going",
        workspace="/ws",
        container_id="running",
        container_name="vibepod-task-running",
        image="vibepod/claude:latest",
        vibepod_version="0.11.0",
    )
    missing = tmp_task_store.create(
        agent="codex",
        prompt="already gone",
        workspace="/ws",
        container_id="missing",
        container_name="vibepod-task-missing",
        image="vibepod/codex:latest",
        vibepod_version="0.11.0",
    )

    class _FakeContainer:
        status = "running"

        def __init__(self) -> None:
            self.remove_force: bool | None = None

        def reload(self) -> None:
            pass

        def remove(self, force: bool = False) -> None:
            self.remove_force = force

    running_container = _FakeContainer()

    class _Manager:
        def get_container(self, name_or_id: str):
            from vibepod.core.docker import DockerClientError

            if name_or_id == "missing":
                raise DockerClientError("missing")
            return running_container

    monkeypatch.setattr(task_cmd, "DockerManager", lambda: _Manager())

    task_cmd.task_rm(task_id=None, all_tasks=True, force=True)

    assert running_container.remove_force is True
    assert tmp_task_store.get(running.id) is None
    assert tmp_task_store.get(missing.id) is None


def test_task_resolve_ambiguous_prefix_errors(monkeypatch, tmp_task_store) -> None:
    # Seed many tasks to increase the chance of prefix collision at a single char.
    for i in range(3):
        tmp_task_store.create(
            agent="claude",
            prompt=f"t{i}",
            workspace="/ws",
            container_id=f"c{i}",
            container_name=f"vibepod-task-{i}",
            image="vibepod/claude:latest",
            vibepod_version="0.11.0",
        )
    # "" is rejected as invalid; but a single character that matches multiple ids
    # is rare (first byte of uuid4 hex). Find one deterministically.
    rows = tmp_task_store.list()
    first_chars = [r.id[0] for r in rows]
    # Find a char that appears in at least 2 ids
    candidates = [c for c in set(first_chars) if first_chars.count(c) >= 2]
    if not candidates:
        pytest.skip("No ambiguous single-char prefix in this random sample")

    class _Manager:
        def get_container(self, name_or_id: str):  # pragma: no cover
            raise AssertionError("should not reach docker when resolve fails")

    monkeypatch.setattr(task_cmd, "DockerManager", lambda: _Manager())

    with pytest.raises(typer.Exit) as exc:
        task_cmd.task_rm(task_id=candidates[0], force=False)
    assert exc.value.exit_code == 1


def test_task_resolve_unknown_id_errors(monkeypatch, tmp_task_store) -> None:
    class _Manager:
        def get_container(self, name_or_id: str):  # pragma: no cover
            raise AssertionError

    monkeypatch.setattr(task_cmd, "DockerManager", lambda: _Manager())

    with pytest.raises(typer.Exit) as exc:
        task_cmd.task_rm(task_id="deadbeef", force=False)
    assert exc.value.exit_code == 1
