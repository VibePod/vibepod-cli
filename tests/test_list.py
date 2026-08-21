"""List command tests."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from vibepod.cli import app
from vibepod.commands import list_cmd
from vibepod.constants import AGENT_SHORTCUTS, DEFAULT_IMAGES, SUPPORTED_AGENTS
from vibepod.core.docker import DockerClientError

runner = CliRunner()


class _FakeDocker:
    """Docker with no containers and *local_images* present locally."""

    def __init__(self, local_images=()) -> None:
        self.local = set(local_images)

    def list_managed(self, all_containers: bool = True):  # noqa: ARG002
        return []

    def image_id(self, image: str) -> str | None:
        return f"sha256:{image}" if image in self.local else None


def _fake_docker(monkeypatch, local_images=()) -> None:
    monkeypatch.setattr(list_cmd, "DockerManager", lambda: _FakeDocker(local_images))


def _base_images_present() -> list[str]:
    """Every agent's base image, as on a machine that has already pulled them."""
    return list(DEFAULT_IMAGES.values())


def _write_overlay(workspace: Path, *parts: str) -> Path:
    dockerfile = workspace.joinpath(".vibepod", "overlay", *parts, "Dockerfile")
    dockerfile.parent.mkdir(parents=True, exist_ok=True)
    dockerfile.write_text("RUN true\n", encoding="utf-8")
    return dockerfile


def test_list_json_includes_short_and_full_agent_names(monkeypatch) -> None:
    class _FakeDockerManager:
        def list_managed(self, all_containers: bool = True):  # noqa: ARG002
            return []

    monkeypatch.setattr(list_cmd, "DockerManager", _FakeDockerManager)

    result = runner.invoke(app, ["list", "--json"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["running"] == []

    rows = payload["agents"]
    by_agent = {row["agent"]: row for row in rows}
    assert set(by_agent.keys()) == set(SUPPORTED_AGENTS)

    for shortcut, agent in AGENT_SHORTCUTS.items():
        assert by_agent[agent]["short"] == shortcut
    assert by_agent["pi"]["short"] == "-"


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

    monkeypatch.setattr(list_cmd, "DockerManager", _FakeDockerManager)

    result = runner.invoke(app, ["list", "--running", "--json"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert "agents" not in payload
    rows = payload["running"]
    assert len(rows) == 2
    assert [row["container"] for row in rows] == ["vibepod-claude-1", "vibepod-claude-2"]
    assert {row["context"] for row in rows} == {"/workspace/a", "/workspace/b"}
    assert all(set(row) == {"agent", "container", "context"} for row in rows)


def test_list_json_reports_project_overlay_image(monkeypatch, tmp_path: Path) -> None:
    """The opaque overlay hash in `docker images` is traceable back to a project."""
    _fake_docker(monkeypatch, _base_images_present())
    workspace = tmp_path / "my-project"
    _write_overlay(workspace, "claude")
    monkeypatch.chdir(workspace)

    result = runner.invoke(app, ["list", "--json"])
    assert result.exit_code == 0

    (row,) = json.loads(result.stdout)["overlays"]
    assert row["agent"] == "claude"
    assert row["image"].startswith("localhost/vibepod/overlay-claude-my-project:")
    assert row["state"] == "not built"


def test_list_json_reports_built_overlay_image(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "my-project"
    _write_overlay(workspace, "claude")
    monkeypatch.chdir(workspace)
    _fake_docker(monkeypatch, _base_images_present())

    # Learn the tag the project resolves to, then present it as already built.
    (pending,) = json.loads(runner.invoke(app, ["list", "--json"]).stdout)["overlays"]
    _fake_docker(monkeypatch, [*_base_images_present(), pending["image"]])

    (row,) = json.loads(runner.invoke(app, ["list", "--json"]).stdout)["overlays"]
    assert (row["image"], row["state"]) == (pending["image"], "built")


def test_list_json_names_only_the_repository_when_base_image_is_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """`vp run` pulls the base first, so its id — and the digest — is unknowable here."""
    _fake_docker(monkeypatch)
    workspace = tmp_path / "my-project"
    _write_overlay(workspace, "claude")
    monkeypatch.chdir(workspace)

    result = runner.invoke(app, ["list", "--json"])
    assert result.exit_code == 0

    (row,) = json.loads(result.stdout)["overlays"]
    assert (row["image"], row["state"]) == (
        "localhost/vibepod/overlay-claude-my-project",
        "base not pulled",
    )


def test_list_json_reports_shared_overlay_for_every_agent(monkeypatch, tmp_path: Path) -> None:
    _fake_docker(monkeypatch, _base_images_present())
    workspace = tmp_path / "my-project"
    _write_overlay(workspace)
    monkeypatch.chdir(workspace)

    result = runner.invoke(app, ["list", "--json"])
    assert result.exit_code == 0

    rows = json.loads(result.stdout)["overlays"]
    assert {row["agent"] for row in rows} == set(SUPPORTED_AGENTS)
    # One image per agent: the fragment sits on a different base image each time.
    assert len({row["image"] for row in rows}) == len(rows)


def test_list_json_marks_disabled_overlay(monkeypatch, tmp_path: Path) -> None:
    _fake_docker(monkeypatch, _base_images_present())
    workspace = tmp_path / "my-project"
    _write_overlay(workspace, "claude")
    (workspace / ".vibepod" / "config.yaml").write_text(
        "agents:\n  claude:\n    overlay: false\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(workspace)

    result = runner.invoke(app, ["list", "--json"])
    assert result.exit_code == 0

    (row,) = json.loads(result.stdout)["overlays"]
    assert (row["agent"], row["image"], row["state"]) == ("claude", "-", "disabled")


def test_list_json_reports_overlays_without_docker(monkeypatch, tmp_path: Path) -> None:
    """Config answers `disabled` on its own; the rest is undecidable, not absent."""

    def _no_docker():
        raise DockerClientError("docker is not running")

    monkeypatch.setattr(list_cmd, "DockerManager", _no_docker)
    workspace = tmp_path / "my-project"
    _write_overlay(workspace, "claude")
    _write_overlay(workspace, "qwen")
    (workspace / ".vibepod" / "config.yaml").write_text(
        "agents:\n  claude:\n    overlay: false\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(workspace)

    result = runner.invoke(app, ["list", "--json"])
    assert result.exit_code == 0

    rows = {row["agent"]: row for row in json.loads(result.stdout)["overlays"]}
    assert rows["claude"]["state"] == "disabled"
    assert rows["qwen"]["state"] == "docker unavailable"


def test_list_json_omits_overlays_without_project_overlay(monkeypatch, tmp_path: Path) -> None:
    _fake_docker(monkeypatch, _base_images_present())
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["list", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["overlays"] == []
