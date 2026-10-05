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
    exit_code: int = 0,
    data: Any | None = None,
    stderr: str = "",
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
    for sub in ("add", "delete", "list", "sync", "update", "export", "cache"):
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
            data=[{"command": "add", "id": "researcher", "name": "Researcher", "path": "/x"}],
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
        },
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


def test_skills_add_json_stdout_is_json_only(monkeypatch: pytest.MonkeyPatch) -> None:
    data = [{"command": "add", "id": "researcher", "name": "Researcher", "path": "/x"}]
    monkeypatch.setattr(
        skills_engine,
        "add",
        lambda locator, *, scope, skill_id=None, link=False, cwd=None: _fake_result(data=data),
    )

    result = runner.invoke(app, ["skills", "add", "./skills/researcher", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == data
    assert "Adding" not in result.stdout


def test_skills_json_failure_keeps_stdout_parseable_and_stderr_human(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = [{"command": "list", "skills": []}]
    monkeypatch.setattr(
        skills_engine,
        "list_skills",
        lambda scope=None, *, cwd=None: _fake_result(exit_code=2, data=data, stderr="not found"),
    )

    result = runner.invoke(app, ["skills", "list", "--json"])

    assert result.exit_code == 2
    assert json.loads(result.stdout) == data
    assert "not found" in result.stderr
    assert "not found" not in result.stdout


def test_skills_delete_propagates_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_delete(
        skill_id: str,
        *,
        scope: str,
        cwd: Path | None = None,
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


def test_skills_cache_clear_invokes_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []

    def fake_cache_clear() -> skills_engine.EngineResult:
        called.append(True)
        return _fake_result(data=[{"command": "cache clear", "cleared": [], "entries": 2}])

    monkeypatch.setattr(skills_engine, "cache_clear", fake_cache_clear)
    result = runner.invoke(app, ["skills", "cache", "clear"])
    assert result.exit_code == 0, result.stdout
    assert called == [True]
    assert "Cleared 2 cached source(s)" in result.stdout


def test_skills_cache_clear_propagates_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_cache_clear() -> skills_engine.EngineResult:
        return _fake_result(exit_code=1, stderr="error: unknown command 'cache'")

    monkeypatch.setattr(skills_engine, "cache_clear", fake_cache_clear)
    result = runner.invoke(app, ["skills", "cache", "clear"])
    assert result.exit_code == 1


def test_detect_scope_default_outside_project(tmp_path: Path) -> None:
    assert skills_engine.detect_scope_default(tmp_path) == "user"


def test_detect_scope_default_inside_project(tmp_path: Path) -> None:
    (tmp_path / ".vibepod").mkdir()
    sub = tmp_path / "sub"
    sub.mkdir()
    assert skills_engine.detect_scope_default(sub) == "local"


def _install(root: Path, skill_id: str, body: str) -> Path:
    skill = root / "installed" / skill_id
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(body, encoding="utf-8")
    lock_path = root / "skills-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8")) if lock_path.exists() else {}
    lock.setdefault("skills", {})[skill_id] = {"path": f"installed/{skill_id}"}
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    return skill


@pytest.fixture
def skill_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    local_root = tmp_path / "local-skills"
    user_root = tmp_path / "user-skills"
    local_root.mkdir()
    user_root.mkdir()
    monkeypatch.setattr(skills_engine, "local_skills_dir", lambda workspace: local_root)
    monkeypatch.setattr(skills_engine, "user_skills_dir", lambda: user_root)
    return local_root, user_root


def test_skills_export_defaults_to_current_directory(
    skill_roots: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_root, user_root = skill_roots
    _install(user_root, "shared", "user version")
    _install(user_root, "only-user", "user only")
    local_skill = _install(local_root, "shared", "local version")
    (local_skill / "scripts").mkdir()
    (local_skill / "scripts" / "run.sh").write_text("echo hi", encoding="utf-8")
    _install(local_root, "not-asked", "x")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)

    result = runner.invoke(app, ["skills", "export", "shared", "only-user"])

    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in work.iterdir()) == ["only-user", "shared"]
    assert (work / "shared" / "SKILL.md").read_text(encoding="utf-8") == "local version"
    assert (work / "shared" / "scripts" / "run.sh").read_text(encoding="utf-8") == "echo hi"
    assert (work / "only-user" / "SKILL.md").read_text(encoding="utf-8") == "user only"


def test_skills_export_scope_and_path(skill_roots: tuple[Path, Path], tmp_path: Path) -> None:
    local_root, user_root = skill_roots
    _install(user_root, "shared", "user version")
    _install(local_root, "shared", "local version")
    dest = tmp_path / "out"

    result = runner.invoke(
        app, ["skills", "export", "shared", "--scope", "user", "--path", str(dest), "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert [(s["id"], s["scope"]) for s in payload[0]["skills"]] == [("shared", "user")]
    assert [p.name for p in dest.iterdir()] == ["shared"]
    assert (dest / "shared" / "SKILL.md").read_text(encoding="utf-8") == "user version"


def test_skills_export_requires_skill_ids(skill_roots: tuple[Path, Path]) -> None:
    result = runner.invoke(app, ["skills", "export"])
    assert result.exit_code == 2


def test_skills_export_unknown_id_fails_without_writing(
    skill_roots: tuple[Path, Path], tmp_path: Path
) -> None:
    _install(skill_roots[0], "known", "x")
    dest = tmp_path / "out"

    result = runner.invoke(app, ["skills", "export", "known", "nope", "--path", str(dest)])

    assert result.exit_code == 1
    assert "nope" in result.output
    assert not dest.exists()


def test_skills_export_refuses_overwrite_unless_forced(
    skill_roots: tuple[Path, Path], tmp_path: Path
) -> None:
    _install(skill_roots[0], "alpha", "new alpha")
    _install(skill_roots[0], "beta", "new beta")
    dest = tmp_path / "out"
    (dest / "alpha").mkdir(parents=True)
    (dest / "alpha" / "stale.md").write_text("old", encoding="utf-8")
    args = ["skills", "export", "alpha", "beta", "--path", str(dest)]

    result = runner.invoke(app, args)
    assert result.exit_code == 1
    assert "--force" in result.output
    assert not (dest / "beta").exists()

    result = runner.invoke(app, [*args, "--force"])
    assert result.exit_code == 0, result.output
    assert not (dest / "alpha" / "stale.md").exists()
    assert (dest / "alpha" / "SKILL.md").read_text(encoding="utf-8") == "new alpha"
    assert (dest / "beta" / "SKILL.md").read_text(encoding="utf-8") == "new beta"


def test_skills_export_keeps_symlinks_as_links(
    skill_roots: tuple[Path, Path], tmp_path: Path
) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("host secret", encoding="utf-8")
    skill = _install(skill_roots[0], "linky", "x")
    (skill / "leak").symlink_to(secret)
    dest = tmp_path / "out"

    result = runner.invoke(app, ["skills", "export", "linky", "--path", str(dest)])

    assert result.exit_code == 0, result.output
    assert (dest / "linky" / "leak").is_symlink()


def test_skills_export_rejects_destination_inside_a_skill(
    skill_roots: tuple[Path, Path], tmp_path: Path
) -> None:
    skill = _install(skill_roots[0], "alpha", "x")

    result = runner.invoke(app, ["skills", "export", "alpha", "--path", str(skill / "out")])

    assert result.exit_code == 1
    assert "inside skill" in result.output
