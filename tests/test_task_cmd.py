"""Task command tests."""

from __future__ import annotations

import sys
from pathlib import Path

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


@pytest.fixture
def tmp_task_store(tmp_path, monkeypatch):
    """Redirect the task store to a fresh tmp path."""
    db_path = tmp_path / "tasks.db"
    monkeypatch.setattr(task_cmd, "_task_store", lambda: TaskStore(db_path))
    return TaskStore(db_path)


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
# task run — agent validation
# ---------------------------------------------------------------------------


def test_task_run_rejects_unknown_agent(monkeypatch, tmp_path, tmp_task_store) -> None:
    monkeypatch.setattr(task_cmd, "get_config", _make_config)

    with pytest.raises(typer.Exit) as exc:
        task_cmd.task_run(agent="nosuchthing", prompt="hi", workspace=tmp_path)
    assert exc.value.exit_code == 1


def test_task_run_rejects_agent_without_headless_prefix(
    monkeypatch, tmp_path, tmp_task_store
) -> None:
    monkeypatch.setattr(task_cmd, "get_config", _make_config)

    with pytest.raises(typer.Exit) as exc:
        task_cmd.task_run(agent="gemini", prompt="hi", workspace=tmp_path)
    assert exc.value.exit_code == 1


# ---------------------------------------------------------------------------
# task run — happy path, command shape
# ---------------------------------------------------------------------------


def test_task_run_claude_builds_headless_command(monkeypatch, tmp_path, tmp_task_store) -> None:
    stub = _CapturingDockerManager()
    monkeypatch.setattr(task_cmd, "get_config", _make_config)
    monkeypatch.setattr(task_cmd, "DockerManager", lambda: stub)

    task_cmd.task_run(agent="claude", prompt="do the thing", workspace=tmp_path)

    assert stub.run_kwargs is not None
    assert stub.run_kwargs["command"] == ["claude", "-p", "do the thing"]
    assert stub.run_kwargs["auto_remove"] is False
    assert stub.run_kwargs["agent"] == "claude"


def test_task_run_codex_uses_exec_subcommand(monkeypatch, tmp_path, tmp_task_store) -> None:
    stub = _CapturingDockerManager()
    monkeypatch.setattr(task_cmd, "get_config", _make_config)
    monkeypatch.setattr(task_cmd, "DockerManager", lambda: stub)

    task_cmd.task_run(agent="codex", prompt="refactor auth", workspace=tmp_path)

    assert stub.run_kwargs["command"] == ["codex", "exec", "refactor auth"]


def test_task_run_auggie_uses_print_flag(monkeypatch, tmp_path, tmp_task_store) -> None:
    stub = _CapturingDockerManager()
    monkeypatch.setattr(task_cmd, "get_config", _make_config)
    monkeypatch.setattr(task_cmd, "DockerManager", lambda: stub)

    task_cmd.task_run(agent="auggie", prompt="run tests", workspace=tmp_path)

    assert stub.run_kwargs["command"] == ["auggie", "--print", "run tests"]


def test_task_run_ikwid_appends_ikwid_args_before_headless_prefix(
    monkeypatch, tmp_path, tmp_task_store
) -> None:
    stub = _CapturingDockerManager()
    monkeypatch.setattr(task_cmd, "get_config", _make_config)
    monkeypatch.setattr(task_cmd, "DockerManager", lambda: stub)

    task_cmd.task_run(
        agent="claude", prompt="do it", workspace=tmp_path, ikwid=True
    )

    assert stub.run_kwargs["command"] == [
        "claude",
        "--dangerously-skip-permissions",
        "-p",
        "do it",
    ]


def test_task_run_passthrough_args_appended_after_prompt(
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
            "run",
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


def test_task_run_records_task_in_store(monkeypatch, tmp_path, tmp_task_store) -> None:
    stub = _CapturingDockerManager()
    monkeypatch.setattr(task_cmd, "get_config", _make_config)
    monkeypatch.setattr(task_cmd, "DockerManager", lambda: stub)

    task_cmd.task_run(agent="claude", prompt="do a thing", workspace=tmp_path)

    rows = tmp_task_store.list()
    assert len(rows) == 1
    assert rows[0].agent == "claude"
    assert rows[0].prompt == "do a thing"
    assert rows[0].container_id == "cid123456789012"


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
    # Make DockerManager unavailable so list falls back to "?" status (no network calls).
    from vibepod.core.docker import DockerClientError

    class _Unavailable:
        def __init__(self) -> None:
            raise DockerClientError("no docker")

    monkeypatch.setattr(task_cmd, "DockerManager", _Unavailable)

    result = CliRunner().invoke(app, ["task", "list", "--json"])
    assert result.exit_code == 0, result.output
    assert '"agent": "claude"' in result.output
    assert '"prompt": "do a"' in result.output


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


def test_task_rm_refuses_running_container_without_force(
    monkeypatch, tmp_task_store
) -> None:
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
