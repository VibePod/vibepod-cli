"""Smoke tests against a real container runtime (Docker or Podman).

Deselected by default; run with: pytest -m integration
The runtime is selected via DOCKER_HOST — Podman's docker-compatible
socket works transparently with docker-py.
"""

from __future__ import annotations

import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from vibepod.core.docker import DockerClientError, DockerManager

pytestmark = pytest.mark.integration

SMOKE_IMAGE = "alpine:3.20"
SMOKE_AGENT = "integration-smoke"


@pytest.fixture()
def manager() -> DockerManager:
    try:
        return DockerManager()
    except DockerClientError as exc:
        pytest.fail(f"Container runtime not reachable: {exc}")


@pytest.fixture()
def mountable_tmp_path() -> Iterator[Path]:
    """Temp dir the runtime can bind-mount.

    On macOS the daemon runs inside a VM (podman machine / colima) that only
    shares $HOME by default; pytest's tmp_path under /private/var/folders is
    not visible to it, so anchor temp dirs under $HOME there.
    """
    base: Path | None = None
    if sys.platform == "darwin":
        base = Path.home() / ".vibepod-integration-tmp"
        base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=base) as tmp:
        yield Path(tmp)


@pytest.fixture()
def cleanup_smoke_containers(manager: DockerManager) -> Iterator[None]:
    yield
    for container in manager.list_managed(all_containers=True):
        if container.labels.get("vibepod.agent") != SMOKE_AGENT:
            continue
        try:
            container.remove(force=True)
        except Exception:
            pass


def _wait_for_log(container: Any, needle: bytes, timeout: float = 30.0) -> bytes:
    deadline = time.monotonic() + timeout
    logs = b""
    while time.monotonic() < deadline:
        container.reload()
        logs = container.logs()
        if needle in logs:
            return logs
        if container.status in ("exited", "dead"):
            # The container died; grab the final logs before deciding, since
            # the needle may have been written just before exit.
            logs = container.logs()
            if needle in logs:
                return logs
            exit_code = container.attrs.get("State", {}).get("ExitCode")
            raise AssertionError(
                f"Container {container.status} (exit code {exit_code}) "
                f"before {needle!r} appeared in logs: {logs!r}"
            )
        time.sleep(0.5)
    raise AssertionError(f"Timed out waiting for {needle!r} in logs: {logs!r}")


def test_pull_run_mount_stop(
    manager: DockerManager,
    mountable_tmp_path: Path,
    cleanup_smoke_containers: None,
) -> None:
    workspace = mountable_tmp_path / "workspace"
    config_dir = mountable_tmp_path / "config"
    workspace.mkdir()
    config_dir.mkdir()
    marker = f"vibepod-smoke-{uuid.uuid4().hex}"
    file_marker = f"file-{marker}"
    env_marker = f"env-{marker}"
    (workspace / "hello.txt").write_text(file_marker + "\n")

    manager.pull_image(SMOKE_IMAGE)

    container = manager.run_agent(
        agent=SMOKE_AGENT,
        image=SMOKE_IMAGE,
        workspace=workspace,
        config_dir=config_dir,
        config_mount_path="/config",
        env={"VIBEPOD_SMOKE": env_marker},
        command=["sh", "-c", 'echo "$VIBEPOD_SMOKE" && cat /workspace/hello.txt && sleep 120'],
        auto_remove=False,
        name=f"vibepod-{SMOKE_AGENT}-{uuid.uuid4().hex[:8]}",
        version="integration-test",
    )

    # Env injection: the echoed env value must land in the logs.
    logs = _wait_for_log(container, env_marker.encode())
    assert env_marker.encode() in logs
    # Workspace bind-mount: the file content is distinct from the env value.
    logs = _wait_for_log(container, file_marker.encode())
    assert file_marker.encode() in logs

    managed_ids = {c.id for c in manager.list_managed()}
    assert container.id in managed_ids

    stopped = manager.stop_agent(SMOKE_AGENT, force=True)
    assert stopped >= 1

    container.reload()
    assert container.status != "running"
