"""Tests for per-project image overlays (core/overlay.py)."""

from __future__ import annotations

import io
import os
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


def _hash(base_ref: str, dockerfile: Path, base_image: str = "base:latest") -> str:
    """Hash a context the way apply_overlay does: over the snapshot tar."""
    return overlay.overlay_hash(
        base_ref,
        overlay.build_context_tar(base_image, dockerfile).getvalue(),
    )


def test_overlay_hash_is_stable(tmp_path: Path) -> None:
    dockerfile = _make_overlay(tmp_path)
    first = _hash("sha256:abc", dockerfile)
    second = _hash("sha256:abc", dockerfile)
    assert first == second
    assert len(first) == 12


def test_overlay_hash_changes_with_base_image(tmp_path: Path) -> None:
    dockerfile = _make_overlay(tmp_path)
    assert _hash("sha256:abc", dockerfile) != _hash("sha256:def", dockerfile)


def test_overlay_hash_changes_with_fragment(tmp_path: Path) -> None:
    dockerfile = _make_overlay(tmp_path)
    before = _hash("sha256:abc", dockerfile)
    dockerfile.write_bytes(b"RUN apt-get update\n")
    assert _hash("sha256:abc", dockerfile) != before


def test_overlay_hash_includes_context_files(tmp_path: Path) -> None:
    dockerfile = _make_overlay(tmp_path)
    before = _hash("sha256:abc", dockerfile)
    (dockerfile.parent / "requirements.txt").write_bytes(b"requests\n")
    assert _hash("sha256:abc", dockerfile) != before


def test_overlay_hash_changes_when_context_file_renamed(tmp_path: Path) -> None:
    dockerfile = _make_overlay(tmp_path)
    extra = dockerfile.parent / "a.txt"
    extra.write_bytes(b"data\n")
    before = _hash("sha256:abc", dockerfile)
    extra.rename(dockerfile.parent / "b.txt")
    assert _hash("sha256:abc", dockerfile) != before


def test_overlay_hash_walks_nested_context_dirs(tmp_path: Path) -> None:
    dockerfile = _make_overlay(tmp_path)
    before = _hash("sha256:abc", dockerfile)
    nested = dockerfile.parent / "scripts"
    nested.mkdir()
    (nested / "setup.sh").write_bytes(b"echo hi\n")
    assert _hash("sha256:abc", dockerfile) != before


def test_shared_overlay_hash_excludes_agent_subdirs(tmp_path: Path) -> None:
    shared = _make_overlay(tmp_path)
    before = _hash("sha256:abc", shared)
    _make_overlay(tmp_path, "gemini", content="RUN apt-get install -y jq\n")
    assert _hash("sha256:abc", shared) == before


def test_overlay_hash_distinguishes_file_boundaries(tmp_path: Path) -> None:
    """Same concatenated bytes split differently across files must not collide."""
    ws_a = tmp_path / "a"
    ws_b = tmp_path / "b"
    dockerfile_a = _make_overlay(ws_a)
    dockerfile_b = _make_overlay(ws_b)
    (dockerfile_a.parent / "f").write_bytes(b"hellog\n-world")
    (dockerfile_b.parent / "f").write_bytes(b"hello")
    (dockerfile_b.parent / "g").write_bytes(b"world")
    assert _hash("sha256:abc", dockerfile_a) != _hash("sha256:abc", dockerfile_b)


def test_overlay_image_tag_is_fully_qualified() -> None:
    # An unqualified name lets Podman normalize it differently at build
    # (docker.io/...) and lookup (localhost/...); the explicit localhost
    # registry makes the name literal for both Docker and Podman.
    assert overlay.overlay_image_tag("claude", "abc123def456") == (
        "localhost/vibepod/overlay-claude:abc123def456"
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


def _symlink_or_skip(target: Path, link: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):  # pragma: no cover - Windows without privilege
        pytest.skip("platform does not allow creating symlinks")


def test_symlinks_excluded_from_hash_and_tar(tmp_path: Path) -> None:
    dockerfile = _make_overlay(tmp_path)
    secret = tmp_path / "outside.txt"
    secret.write_bytes(b"secret\n")
    before = _hash("sha256:abc", dockerfile)
    _symlink_or_skip(secret, dockerfile.parent / "link.txt")
    assert _hash("sha256:abc", dockerfile) == before
    entries = _read_tar(overlay.build_context_tar("base:latest", dockerfile))
    assert "link.txt" not in entries


def test_files_under_symlinked_dir_excluded(tmp_path: Path) -> None:
    dockerfile = _make_overlay(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data.txt").write_bytes(b"data\n")
    before = _hash("sha256:abc", dockerfile)
    _symlink_or_skip(outside, dockerfile.parent / "linked")
    assert _hash("sha256:abc", dockerfile) == before
    entries = _read_tar(overlay.build_context_tar("base:latest", dockerfile))
    assert "linked/data.txt" not in entries


def test_find_overlay_dockerfile_rejects_symlinked_dockerfile(tmp_path: Path) -> None:
    outside = tmp_path / "outside-dockerfile"
    outside.write_bytes(b"RUN echo outside\n")
    link = tmp_path / ".vibepod" / "overlay" / "Dockerfile"
    link.parent.mkdir(parents=True)
    _symlink_or_skip(outside, link)
    assert overlay.find_overlay_dockerfile(tmp_path, "claude") is None


def test_find_overlay_dockerfile_symlinked_agent_falls_back_to_shared(tmp_path: Path) -> None:
    shared = _make_overlay(tmp_path)
    outside = tmp_path / "outside-dockerfile"
    outside.write_bytes(b"RUN echo outside\n")
    agent_link = tmp_path / ".vibepod" / "overlay" / "claude" / "Dockerfile"
    agent_link.parent.mkdir(parents=True)
    _symlink_or_skip(outside, agent_link)
    assert overlay.find_overlay_dockerfile(tmp_path, "claude") == shared


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

    def build_image(self, context_tar, tag, labels, nocache=False):
        self.built.append((tag, labels, context_tar, nocache))
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
    assert result.startswith("localhost/vibepod/overlay-claude:")
    (built,) = manager.built
    assert built[0] == result
    assert built[1]["vibepod.managed"] == "true"
    assert built[3] is False
    assert manager.swept == [(built[1]["vibepod.overlay.key"], result)]


def test_apply_overlay_builds_the_snapshot_it_hashed(tmp_path: Path) -> None:
    """The tar handed to docker must be the exact bytes the tag was derived from."""
    _make_overlay(tmp_path)
    manager = _FakeManager(image_ids={"base:latest": "sha256:base"})
    result = overlay.apply_overlay(manager, tmp_path, "claude", "base:latest")
    ((tag, _, context_tar, _),) = manager.built
    digest = overlay.overlay_hash("sha256:base", context_tar.getvalue())
    assert tag == overlay.overlay_image_tag("claude", digest) == result


def test_apply_overlay_skips_build_when_image_exists(tmp_path: Path) -> None:
    dockerfile = _make_overlay(tmp_path)
    digest = _hash("sha256:base", dockerfile)
    tag = overlay.overlay_image_tag("claude", digest)
    manager = _FakeManager(existing_images=[tag], image_ids={"base:latest": "sha256:base"})
    assert overlay.apply_overlay(manager, tmp_path, "claude", "base:latest") == tag
    assert manager.built == []


def test_apply_overlay_rebuild_forces_uncached_build(tmp_path: Path) -> None:
    dockerfile = _make_overlay(tmp_path)
    digest = _hash("sha256:base", dockerfile)
    tag = overlay.overlay_image_tag("claude", digest)
    manager = _FakeManager(existing_images=[tag], image_ids={"base:latest": "sha256:base"})
    assert overlay.apply_overlay(manager, tmp_path, "claude", "base:latest", rebuild=True) == tag
    ((built_tag, _, _, nocache),) = manager.built
    assert built_tag == tag
    assert nocache is True


def test_apply_overlay_hashes_tag_when_base_not_local(tmp_path: Path) -> None:
    dockerfile = _make_overlay(tmp_path)
    manager = _FakeManager()
    result = overlay.apply_overlay(manager, tmp_path, "claude", "base:latest")
    assert result == overlay.overlay_image_tag("claude", _hash("base:latest", dockerfile))


def test_apply_overlay_key_differs_per_workspace_and_agent(tmp_path: Path) -> None:
    ws_a = tmp_path / "a"
    ws_b = tmp_path / "b"
    _make_overlay(ws_a)
    _make_overlay(ws_b)
    manager_a = _FakeManager()
    manager_b = _FakeManager()
    overlay.apply_overlay(manager_a, ws_a, "claude", "base:latest")
    overlay.apply_overlay(manager_b, ws_b, "claude", "base:latest")
    ((_, labels_a, _, _),) = manager_a.built
    ((_, labels_b, _, _),) = manager_b.built
    assert labels_a["vibepod.overlay.key"] != labels_b["vibepod.overlay.key"]


def test_apply_overlay_if_enabled_calls_core(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        launch.overlay,
        "apply_overlay",
        lambda *a, **k: pytest.fail("should not build"),
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
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        launch.overlay,
        "apply_overlay",
        lambda *a, **k: pytest.fail("should not build"),
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


def _write_project_config(workspace: Path, content: str) -> None:
    config_path = workspace / ".vibepod" / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(content, encoding="utf-8")


def test_apply_overlay_if_enabled_respects_workspace_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """agents.<agent>.overlay: false in the *workspace* project config wins,
    even when get_config() was resolved from a different current directory."""
    monkeypatch.setattr(
        launch.overlay,
        "apply_overlay",
        lambda *a, **k: pytest.fail("should not build"),
    )
    _write_project_config(tmp_path, "agents:\n  claude:\n    overlay: false\n")
    result = launch.apply_overlay_if_enabled(
        manager="mgr",
        workspace_path=tmp_path,
        agent="claude",
        image="base:latest",
        agent_cfg={},
        no_overlay=False,
        rebuild_overlay=False,
    )
    assert result == "base:latest"


def test_apply_overlay_if_enabled_workspace_config_overrides_cwd_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        launch.overlay,
        "apply_overlay",
        lambda *a, **k: "vibepod/overlay-claude:abc",
    )
    _write_project_config(tmp_path, "agents:\n  claude:\n    overlay: true\n")
    result = launch.apply_overlay_if_enabled(
        manager="mgr",
        workspace_path=tmp_path,
        agent="claude",
        image="base:latest",
        agent_cfg={"overlay": False},
        no_overlay=False,
        rebuild_overlay=False,
    )
    assert result == "vibepod/overlay-claude:abc"


def test_apply_overlay_if_enabled_exits_on_build_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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


@pytest.mark.skipif(os.name == "nt", reason="exec bits are POSIX-only")
def test_overlay_hash_changes_with_exec_bit(tmp_path: Path) -> None:
    dockerfile = _make_overlay(tmp_path, content="COPY setup.sh /setup.sh\n")
    script = dockerfile.parent / "setup.sh"
    script.write_bytes(b"#!/bin/sh\n")
    script.chmod(0o644)
    before = _hash("sha256:abc", dockerfile)

    script.chmod(0o755)

    assert _hash("sha256:abc", dockerfile) != before


@pytest.mark.skipif(os.name == "nt", reason="exec bits are POSIX-only")
def test_build_context_tar_normalizes_member_metadata(tmp_path: Path) -> None:
    dockerfile = _make_overlay(tmp_path, content="COPY . /overlay\n")
    script = dockerfile.parent / "setup.sh"
    script.write_bytes(b"#!/bin/sh\n")
    script.chmod(0o750)
    data = dockerfile.parent / "config.txt"
    data.write_bytes(b"x\n")
    data.chmod(0o600)

    buffer = overlay.build_context_tar("base:latest", dockerfile)

    with tarfile.open(fileobj=buffer) as archive:
        members = {member.name: member for member in archive.getmembers()}
    assert members["setup.sh"].mode == 0o755
    assert members["config.txt"].mode == 0o644
    for member in members.values():
        assert (member.uid, member.gid, member.uname, member.gname) == (0, 0, "", "")
        assert member.mtime == 0
