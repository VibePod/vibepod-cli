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

    monkeypatch.setattr(list_cmd, "DockerManager", _FakeDockerManager)

    result = runner.invoke(app, ["list", "--json"])
    assert result.exit_code == 0

    rows = json.loads(result.stdout)
    by_agent = {row["agent"]: row for row in rows}
    assert set(by_agent.keys()) == set(SUPPORTED_AGENTS)

    for shortcut, agent in AGENT_SHORTCUTS.items():
        assert by_agent[agent]["short"] == shortcut
