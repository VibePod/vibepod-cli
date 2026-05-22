"""Tests for `vp skills` — exercise the host-side driver with the engine mocked."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from vibepod.cli import app
from vibepod.core import skills_engine

runner = CliRunner()


def _fake_result(
    exit_code: int = 0, data: Any | None = None, stderr: str = ""
) -> skills_engine.EngineResult:
    return skills_engine.EngineResult(
        exit_code=exit_code,
        stdout=json.dumps(data) if data is not None else "",
        stderr=stderr,
        data=data,
    )


def test_skills_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["skills", "--help"])
    assert result.exit_code == 0
    for sub in ("add", "delete", "list", "sync", "update"):
        assert sub in result.stdout


def test_skills_add_invokes_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_add(
        locator: str,
        *,
        scope: str,
        skill_id: str | None = None,
        link: bool = False,
        cwd: Path | None = None,
    ) -> skills_engine.EngineResult:
        seen.update(locator=locator, scope=scope, skill_id=skill_id, link=link)
        return _fake_result(
            data=[{"command": "add", "id": "researcher", "name": "Researcher", "path": "/x"}]
        )

    monkeypatch.setattr(skills_engine, "add", fake_add)
    result = runner.invoke(app, ["skills", "add", "./skills/researcher", "--scope", "local"])
    assert result.exit_code == 0, result.stdout
    assert seen["locator"] == "./skills/researcher"
    assert seen["scope"] == "local"
    assert seen["link"] is False


def test_skills_list_renders_table(monkeypatch: pytest.MonkeyPatch) -> None:
    data = [
        {
            "command": "list",
            "skills": [
                {
                    "id": "sql",
                    "name": "SQL Helper",
                    "version": "1.0.0",
                    "scope": "local",
                    "status": "active",
                },
                {
                    "id": "sql",
                    "name": "SQL Helper",
                    "version": "0.9.0",
                    "scope": "user",
                    "status": "shadowed",
                    "shadowedBy": "local",
                },
            ],
        }
    ]

    def fake_list(scope: Any = None, *, cwd: Any = None) -> skills_engine.EngineResult:
        return _fake_result(data=data)

    monkeypatch.setattr(skills_engine, "list_skills", fake_list)
    result = runner.invoke(app, ["skills", "list"])
    assert result.exit_code == 0
    assert "sql" in result.stdout
    assert "shadowed by local" in result.stdout


def test_skills_list_json_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    data = [{"command": "list", "skills": []}]
    monkeypatch.setattr(
        skills_engine,
        "list_skills",
        lambda scope=None, *, cwd=None: _fake_result(data=data),
    )
    result = runner.invoke(app, ["skills", "list", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == data


def test_skills_delete_propagates_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_delete(
        skill_id: str, *, scope: str, cwd: Path | None = None
    ) -> skills_engine.EngineResult:
        return _fake_result(exit_code=1, stderr="not found")

    monkeypatch.setattr(skills_engine, "delete", fake_delete)
    result = runner.invoke(app, ["skills", "delete", "missing", "--scope", "local"])
    assert result.exit_code == 1


def test_skills_sync_invokes_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Any] = {}

    def fake_sync(scope: str, *, cwd: Path | None = None) -> skills_engine.EngineResult:
        called["scope"] = scope
        return _fake_result(data=[{"command": "sync", "restored": [], "unchanged": ["foo"]}])

    monkeypatch.setattr(skills_engine, "sync", fake_sync)
    result = runner.invoke(app, ["skills", "sync", "--scope", "user"])
    assert result.exit_code == 0, result.stdout
    assert called["scope"] == "user"


def test_detect_scope_default_outside_project(tmp_path: Path) -> None:
    assert skills_engine.detect_scope_default(tmp_path) == "user"


def test_detect_scope_default_inside_project(tmp_path: Path) -> None:
    (tmp_path / ".vibepod").mkdir()
    sub = tmp_path / "sub"
    sub.mkdir()
    assert skills_engine.detect_scope_default(sub) == "local"
