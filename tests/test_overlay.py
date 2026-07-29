"""Tests for per-project image overlays (core/overlay.py)."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest
import typer

from vibepod.core import launch, overlay
from vibepod.core.docker import DockerClientError


def _make_overlay(root: Path, *parts: str, content: str = "RUN true\n") -> Path:
    dockerfile = root.joinpath(".vibepod", "overlay", *parts, "Dockerfile")
    dockerfile.parent.mkdir(parents=True, exist_ok=True)
    # write_bytes, not write_text: text mode would turn \n into \r\n on
    # Windows and break the exact-byte assertions on the tar contents.
    dockerfile.write_bytes(content.encode())
    return dockerfile


def test_find_overlay_dockerfile_returns_none_without_overlay(tmp_path: Path) -> None:
    assert overlay.find_overlay_dockerfile(tmp_path, "claude") is None


def test_find_overlay_dockerfile_finds_shared_overlay(tmp_path: Path) -> None:
    dockerfile = _make_overlay(tmp_path)
    assert overlay.find_overlay_dockerfile(tmp_path, "claude") == dockerfile


def test_find_overlay_dockerfile_prefers_agent_overlay(tmp_path: Path) -> None:
    _make_overlay(tmp_path)
    agent_dockerfile = _make_overlay(tmp_path, "claude")
    assert overlay.find_overlay_dockerfile(tmp_path, "claude") == agent_dockerfile


def test_find_overlay_dockerfile_ignores_other_agent_dirs(tmp_path: Path) -> None:
    _make_overlay(tmp_path, "gemini")
    assert overlay.find_overlay_dockerfile(tmp_path, "claude") is None


def test_find_overlay_dockerfile_ignores_directory_named_dockerfile(tmp_path: Path) -> None:
    path = tmp_path / ".vibepod" / "overlay" / "Dockerfile"
    path.mkdir(parents=True)
    assert overlay.find_overlay_dockerfile(tmp_path, "claude") is None


def test_overlay_hash_is_stable(tmp_path: Path) -> None:
    dockerfile = _make_overlay(tmp_path)
    first = overlay.overlay_hash("sha256:abc", dockerfile)
    second = overlay.overlay_hash("sha256:abc", dockerfile)
    assert first == second
    assert len(first) == 12


def test_overlay_hash_changes_with_base_image(tmp_path: Path) -> None:
    dockerfile = _make_overlay(tmp_path)
    assert overlay.overlay_hash("sha256:abc", dockerfile) != overlay.overlay_hash(
        "sha256:def", dockerfile
    )


def test_overlay_hash_changes_with_fragment(tmp_path: Path) -> None:
    dockerfile = _make_overlay(tmp_path)
    before = overlay.overlay_hash("sha256:abc", dockerfile)
    dockerfile.write_bytes(b"RUN apt-get update\n")
    assert overlay.overlay_hash("sha256:abc", dockerfile) != before


def test_overlay_hash_includes_context_files(tmp_path: Path) -> None:
    dockerfile = _make_overlay(tmp_path)
    before = overlay.overlay_hash("sha256:abc", dockerfile)
    (dockerfile.parent / "requirements.txt").write_bytes(b"requests\n")
    assert overlay.overlay_hash("sha256:abc", dockerfile) != before


def test_overlay_hash_changes_when_context_file_renamed(tmp_path: Path) -> None:
    dockerfile = _make_overlay(tmp_path)
    extra = dockerfile.parent / "a.txt"
    extra.write_bytes(b"data\n")
    before = overlay.overlay_hash("sha256:abc", dockerfile)
    extra.rename(dockerfile.parent / "b.txt")
    assert overlay.overlay_hash("sha256:abc", dockerfile) != before


def test_overlay_hash_walks_nested_context_dirs(tmp_path: Path) -> None:
    dockerfile = _make_overlay(tmp_path)
    before = overlay.overlay_hash("sha256:abc", dockerfile)
    nested = dockerfile.parent / "scripts"
    nested.mkdir()
    (nested / "setup.sh").write_bytes(b"echo hi\n")
    assert overlay.overlay_hash("sha256:abc", dockerfile) != before


def test_shared_overlay_hash_excludes_agent_subdirs(tmp_path: Path) -> None:
    shared = _make_overlay(tmp_path)
    before = overlay.overlay_hash("sha256:abc", shared)
    _make_overlay(tmp_path, "gemini", content="RUN apt-get install -y jq\n")
    assert overlay.overlay_hash("sha256:abc", shared) == before


def test_overlay_image_tag_embeds_agent_and_hash() -> None:
    assert overlay.overlay_image_tag("claude", "abc123def456") == (
        "vibepod/overlay-claude:abc123def456"
    )


def _read_tar(buffer: io.BytesIO) -> dict[str, bytes]:
    buffer.seek(0)
    with tarfile.open(fileobj=buffer) as archive:
        entries = {}
        for member in archive.getmembers():
            if not member.isfile():
                continue
            extracted = archive.extractfile(member)
            assert extracted is not None
            entries[member.name] = extracted.read()
        return entries


def test_build_context_tar_synthesizes_dockerfile(tmp_path: Path) -> None:
    dockerfile = _make_overlay(tmp_path, content="RUN apt-get install -y jq\n")
    entries = _read_tar(overlay.build_context_tar("vibepod/claude:latest", dockerfile))
    assert entries["Dockerfile"] == b"FROM vibepod/claude:latest\nRUN apt-get install -y jq\n"


def test_build_context_tar_includes_context_files(tmp_path: Path) -> None:
    dockerfile = _make_overlay(tmp_path)
    (dockerfile.parent / "requirements.txt").write_bytes(b"requests\n")
    nested = dockerfile.parent / "scripts"
    nested.mkdir()
    (nested / "setup.sh").write_bytes(b"echo hi\n")
    entries = _read_tar(overlay.build_context_tar("base:latest", dockerfile))
    assert entries["requirements.txt"] == b"requests\n"
    assert entries["scripts/setup.sh"] == b"echo hi\n"


def test_build_context_tar_excludes_agent_subdirs_for_shared_overlay(tmp_path: Path) -> None:
    shared = _make_overlay(tmp_path)
    _make_overlay(tmp_path, "gemini")
    entries = _read_tar(overlay.build_context_tar("base:latest", shared))
    assert "gemini/Dockerfile" not in entries


class _FakeManager:
    def __init__(self, existing_images=(), image_ids=None):
        self.existing = set(existing_images)
        self.ids = image_ids or {}
        self.built = []
        self.swept = []

    def image_id(self, image):
        if image in self.ids:
            return self.ids[image]
        return "sha256:built" if image in self.existing else None

    def build_image(self, context_tar, tag, labels):
        self.built.append((tag, labels))
        self.existing.add(tag)

    def remove_stale_overlays(self, overlay_key, keep_tag):
        self.swept.append((overlay_key, keep_tag))
        return 0


def test_apply_overlay_returns_base_image_without_overlay(tmp_path: Path) -> None:
    manager = _FakeManager()
    assert overlay.apply_overlay(manager, tmp_path, "claude", "base:latest") == "base:latest"
    assert manager.built == []


def test_apply_overlay_builds_and_returns_overlay_tag(tmp_path: Path) -> None:
    _make_overlay(tmp_path)
    manager = _FakeManager(image_ids={"base:latest": "sha256:base"})
    result = overlay.apply_overlay(manager, tmp_path, "claude", "base:latest")
    assert result.startswith("vibepod/overlay-claude:")
    (built,) = manager.built
    assert built[0] == result
    assert built[1]["vibepod.managed"] == "true"
    assert manager.swept == [(built[1]["vibepod.overlay.key"], result)]


def test_apply_overlay_skips_build_when_image_exists(tmp_path: Path) -> None:
    dockerfile = _make_overlay(tmp_path)
    digest = overlay.overlay_hash("sha256:base", dockerfile)
    tag = overlay.overlay_image_tag("claude", digest)
    manager = _FakeManager(existing_images=[tag], image_ids={"base:latest": "sha256:base"})
    assert overlay.apply_overlay(manager, tmp_path, "claude", "base:latest") == tag
    assert manager.built == []


def test_apply_overlay_rebuild_forces_build(tmp_path: Path) -> None:
    dockerfile = _make_overlay(tmp_path)
    digest = overlay.overlay_hash("sha256:base", dockerfile)
    tag = overlay.overlay_image_tag("claude", digest)
    manager = _FakeManager(existing_images=[tag], image_ids={"base:latest": "sha256:base"})
    assert overlay.apply_overlay(manager, tmp_path, "claude", "base:latest", rebuild=True) == tag
    assert [built_tag for built_tag, _ in manager.built] == [tag]


def test_apply_overlay_hashes_tag_when_base_not_local(tmp_path: Path) -> None:
    dockerfile = _make_overlay(tmp_path)
    manager = _FakeManager()
    result = overlay.apply_overlay(manager, tmp_path, "claude", "base:latest")
    assert result == overlay.overlay_image_tag(
        "claude", overlay.overlay_hash("base:latest", dockerfile)
    )


def test_apply_overlay_key_differs_per_workspace_and_agent(tmp_path: Path) -> None:
    ws_a = tmp_path / "a"
    ws_b = tmp_path / "b"
    _make_overlay(ws_a)
    _make_overlay(ws_b)
    manager_a = _FakeManager()
    manager_b = _FakeManager()
    overlay.apply_overlay(manager_a, ws_a, "claude", "base:latest")
    overlay.apply_overlay(manager_b, ws_b, "claude", "base:latest")
    ((_, labels_a),) = manager_a.built
    ((_, labels_b),) = manager_b.built
    assert labels_a["vibepod.overlay.key"] != labels_b["vibepod.overlay.key"]


def test_apply_overlay_if_enabled_calls_core(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = {}

    def fake_apply(manager, workspace, agent, base_image, *, rebuild=False):
        calls["args"] = (manager, workspace, agent, base_image, rebuild)
        return "vibepod/overlay-claude:abc"

    monkeypatch.setattr(launch.overlay, "apply_overlay", fake_apply)
    result = launch.apply_overlay_if_enabled(
        manager="mgr",
        workspace_path=tmp_path,
        agent="claude",
        image="base:latest",
        agent_cfg={},
        no_overlay=False,
        rebuild_overlay=True,
    )
    assert result == "vibepod/overlay-claude:abc"
    assert calls["args"] == ("mgr", tmp_path, "claude", "base:latest", True)


def test_apply_overlay_if_enabled_respects_no_overlay_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        launch.overlay, "apply_overlay", lambda *a, **k: pytest.fail("should not build")
    )
    result = launch.apply_overlay_if_enabled(
        manager="mgr",
        workspace_path=tmp_path,
        agent="claude",
        image="base:latest",
        agent_cfg={},
        no_overlay=True,
        rebuild_overlay=False,
    )
    assert result == "base:latest"


def test_apply_overlay_if_enabled_respects_agent_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        launch.overlay, "apply_overlay", lambda *a, **k: pytest.fail("should not build")
    )
    result = launch.apply_overlay_if_enabled(
        manager="mgr",
        workspace_path=tmp_path,
        agent="claude",
        image="base:latest",
        agent_cfg={"overlay": False},
        no_overlay=False,
        rebuild_overlay=False,
    )
    assert result == "base:latest"


def test_apply_overlay_if_enabled_exits_on_build_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def failing_apply(*a, **k):
        raise DockerClientError("build broke")

    monkeypatch.setattr(launch.overlay, "apply_overlay", failing_apply)
    with pytest.raises(typer.Exit):
        launch.apply_overlay_if_enabled(
            manager="mgr",
            workspace_path=tmp_path,
            agent="claude",
            image="base:latest",
            agent_cfg={},
            no_overlay=False,
            rebuild_overlay=False,
        )
