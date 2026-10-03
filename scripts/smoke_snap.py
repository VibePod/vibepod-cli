"""Run with the installed snap's Python on a host with a container daemon."""

from __future__ import annotations

import os
import subprocess
import tempfile
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from vibepod.core.docker import DockerManager


def main() -> None:
    assert version("vibepod") == os.environ["VP_SNAP_EXPECTED_VERSION"]
    subprocess.run(["snap", "run", "vibepod", "--help"], check=True)
    manager = DockerManager()
    manager.pull_image("alpine:3.20")
    # A host path outside snap-private storage must be visible to the daemon.
    with tempfile.TemporaryDirectory(prefix="vibepod-snap-", dir=Path.home()) as tmp:
        workspace = Path(tmp)
        (workspace / "marker").write_text("snap-workspace-ok\n")
        container = manager.run_agent(
            agent="snap-smoke",
            image="alpine:3.20",
            workspace=workspace,
            config_dir=workspace,
            config_mount_path="/config",
            env={"VP_SNAP_SMOKE": "snap-env-ok"},
            command=["sh", "-c", 'cat /workspace/marker; echo "$VP_SNAP_SMOKE"'],
            auto_remove=False,
            name=f"vibepod-snap-smoke-{uuid4().hex[:8]}",
            version=version("vibepod"),
        )
        try:
            assert container.wait(timeout=60)["StatusCode"] == 0
            logs = container.logs()
            assert b"snap-workspace-ok" in logs
            assert b"snap-env-ok" in logs
            assert container.id in {c.id for c in manager.list_managed(all_containers=True)}
        finally:
            container.remove(force=True)


if __name__ == "__main__":
    main()
