"""List command tests."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from vibepod.cli import app
from vibepod.commands import list_cmd
from vibepod.constants import AGENT_SHORTCUTS, SUPPORTED_AGENTS

runner = CliRunner()


def test_list_json_includes_short_and_full_agent_names(monkeypatch) -> None:
    class _FakeDockerManager:
        def list_managed(self, all_containers: bool = True):  # noqa: ARG002
            return []

    monkeypatch.setattr(list_cmd, "get_manager", lambda **kwargs: _FakeDockerManager())

    result = runner.invoke(app, ["list", "--json"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["running"] == []

    rows = payload["agents"]
    by_agent = {row["agent"]: row for row in rows}
    assert set(by_agent.keys()) == set(SUPPORTED_AGENTS)

    for shortcut, agent in AGENT_SHORTCUTS.items():
        assert by_agent[agent]["short"] == shortcut


def test_list_running_json_preserves_multiple_instances(monkeypatch) -> None:
    class _FakeContainer:
        def __init__(self, name: str, status: str, labels: dict[str, str]) -> None:
            self.name = name
            self.status = status
            self.labels = labels

    class _FakeDockerManager:
        def list_managed(self, all_containers: bool = True):  # noqa: ARG002
            return [
                _FakeContainer(
                    "vibepod-claude-1",
                    "running",
                    {"vibepod.agent": "claude", "vibepod.workspace": "/workspace/a"},
                ),
                _FakeContainer(
                    "vibepod-claude-2",
                    "running",
                    {"vibepod.agent": "claude", "vibepod.workspace": "/workspace/b"},
                ),
                _FakeContainer(
                    "vibepod-codex-1",
                    "exited",
                    {"vibepod.agent": "codex", "vibepod.workspace": "/workspace/c"},
                ),
            ]

    monkeypatch.setattr(list_cmd, "get_manager", lambda **kwargs: _FakeDockerManager())

    result = runner.invoke(app, ["list", "--running", "--json"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert "agents" not in payload
    rows = payload["running"]
    assert len(rows) == 2
    assert [row["container"] for row in rows] == ["vibepod-claude-1", "vibepod-claude-2"]
    assert {row["context"] for row in rows} == {"/workspace/a", "/workspace/b"}
    assert all(set(row) == {"agent", "container", "context"} for row in rows)
