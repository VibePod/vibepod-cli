"""Logs command tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from vibepod.cli import app
from vibepod.commands import logs
from vibepod.core.session_logger import SessionLogger

runner = CliRunner()


def _config(db_path: Path) -> dict[str, Any]:
    return {
        "logging": {"enabled": True, "db_path": str(db_path)},
        "proxy": {"db_path": str(db_path.parent / "proxy.db")},
    }


def test_logs_show_prints_persisted_task_output(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "logs.db"
    SessionLogger.create_session(
        db_path,
        session_id="task123",
        agent="claude",
        image="image",
        workspace="/workspace",
        container_id="abc123",
        container_name="vibepod-claude-test",
        vibepod_version="0.2.1",
    )
    SessionLogger.append_output(db_path, session_id="task123", content="hello\n")
    monkeypatch.setattr(logs, "get_config", lambda: _config(db_path))

    result = runner.invoke(app, ["logs", "show", "task123"])

    assert result.exit_code == 0
    assert result.stdout == "hello\n"


def test_logs_show_unknown_task_exits(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "logs.db"
    monkeypatch.setattr(logs, "get_config", lambda: _config(db_path))

    result = runner.invoke(app, ["logs", "show", "missing"])

    assert result.exit_code == 1
    assert "Unknown task ID: missing" in result.stdout


def test_logs_show_falls_back_to_docker_logs(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "logs.db"
    SessionLogger.create_session(
        db_path,
        session_id="task123",
        agent="claude",
        image="image",
        workspace="/workspace",
        container_id="abc123",
        container_name="vibepod-claude-test",
        vibepod_version="0.2.1",
    )

    class _FakeDockerManager:
        def container_logs(self, container_id: str) -> str:
            assert container_id == "abc123"
            return "from docker\n"

    monkeypatch.setattr(logs, "get_config", lambda: _config(db_path))
    monkeypatch.setattr(logs, "DockerManager", _FakeDockerManager)

    result = runner.invoke(app, ["logs", "show", "task123"])

    assert result.exit_code == 0
    assert result.stdout == "from docker\n"
