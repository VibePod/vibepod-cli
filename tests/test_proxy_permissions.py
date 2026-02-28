"""Proxy permission and mapping behavior tests."""

from __future__ import annotations

import json
from pathlib import Path

from vibepod.commands import run as run_cmd
from vibepod.core import docker as docker_mod
from vibepod.core.docker import DockerManager


def test_update_container_mapping_success(tmp_path: Path) -> None:
    mapping_path = tmp_path / "proxy" / "containers.json"
    mapping_path.parent.mkdir(parents=True, exist_ok=True)

    updated = run_cmd._update_container_mapping(
        mapping_path,
        "172.18.0.3",
        "abc123",
        "vibepod-claude-test",
        "claude",
    )

    assert updated is True
    data = json.loads(mapping_path.read_text())
    assert data["172.18.0.3"]["container_id"] == "abc123"
    assert data["172.18.0.3"]["container_name"] == "vibepod-claude-test"
    assert data["172.18.0.3"]["agent"] == "claude"


def test_update_container_mapping_permission_error_returns_false(
    tmp_path: Path, monkeypatch
) -> None:
    mapping_path = tmp_path / "proxy" / "containers.json"
    mapping_path.parent.mkdir(parents=True, exist_ok=True)

    def _fail_replace(src: str, dst: str) -> None:
        del src, dst
        raise PermissionError("permission denied")

    monkeypatch.setattr(run_cmd.os, "replace", _fail_replace)

    updated = run_cmd._update_container_mapping(
        mapping_path,
        "172.18.0.4",
        "def456",
        "vibepod-codex-test",
        "codex",
    )
    assert updated is False


def test_ensure_proxy_runs_container_as_current_user(tmp_path: Path, monkeypatch) -> None:
    class _FakeContainers:
        def __init__(self) -> None:
            self.run_kwargs: dict | None = None

        def run(self, **kwargs):
            self.run_kwargs = kwargs
            return {"id": "proxy"}

    class _FakeClient:
        def __init__(self) -> None:
            self.containers = _FakeContainers()

    manager = object.__new__(DockerManager)
    manager.client = _FakeClient()  # type: ignore[assignment]

    monkeypatch.setattr(DockerManager, "find_proxy", lambda self: None)
    monkeypatch.setattr(docker_mod.os, "getuid", lambda: 1234)
    monkeypatch.setattr(docker_mod.os, "getgid", lambda: 2345)

    db_path = tmp_path / "proxy" / "proxy.db"
    ca_dir = tmp_path / "proxy" / "mitmproxy"
    manager.ensure_proxy(
        image="vibepod/proxy:latest",
        db_path=db_path,
        ca_dir=ca_dir,
        network="vibepod-network",
    )

    run_kwargs = manager.client.containers.run_kwargs  # type: ignore[union-attr]
    assert run_kwargs is not None
    assert run_kwargs["user"] == "1234:2345"
    assert "ports" not in run_kwargs
    assert db_path.parent.exists()
    assert ca_dir.exists()
