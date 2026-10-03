"""Run with the installed snap's Python on a host with a container daemon."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from importlib.metadata import version
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

from vibepod.core.docker import DockerManager


def main() -> None:
    snap_root = Path(os.environ["SNAP"]).resolve()
    assert Path(sys.executable).resolve().is_relative_to(snap_root)
    import vibepod

    assert Path(vibepod.__file__).resolve().is_relative_to(snap_root)
    assert version("vibepod") == os.environ["VP_SNAP_EXPECTED_VERSION"]
    for command in ("vibepod", "vibepod.vp"):
        reported = subprocess.run(
            ["snap", "run", command, "version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert reported.stdout.splitlines()[0] == (
            f"VibePod CLI: {os.environ['VP_SNAP_EXPECTED_VERSION']}"
        ), reported.stdout
    subprocess.run(["snap", "run", "vibepod", "--help"], check=True, timeout=30)
    manager = DockerManager()
    manager.pull_image("alpine:3.20")
    # A host path outside snap-private storage must be visible to the daemon.
    with tempfile.TemporaryDirectory(prefix="vibepod-snap-", dir=Path.home()) as tmp:
        workspace = Path(tmp)
        (workspace / "marker").write_text("snap-workspace-ok\n")
        config_dir = workspace / ".config"
        config_dir.mkdir()
        (config_dir / "marker").write_text("snap-config-ok\n")
        container = manager.run_agent(
            agent="snap-smoke",
            image="alpine:3.20",
            workspace=workspace,
            config_dir=config_dir,
            config_mount_path="/config",
            env={"VP_SNAP_SMOKE": "snap-env-ok"},
            command=["httpd", "-f", "-p", "8080", "-h", "/workspace"],
            ports={"8080/tcp": ("127.0.0.1", None)},
            auto_remove=False,
            name=f"vibepod-snap-smoke-{uuid4().hex[:8]}",
            version=version("vibepod"),
        )
        try:
            # Exercise the localhost port publishing used by proxy/dashboard
            # services, using a daemon-assigned port to avoid host conflicts.
            container.reload()
            bindings = container.attrs["NetworkSettings"]["Ports"]["8080/tcp"]
            port = bindings[0]["HostPort"]
            deadline = time.monotonic() + 30
            while True:
                try:
                    with urlopen(f"http://127.0.0.1:{port}/marker", timeout=2) as response:
                        assert response.read() == b"snap-workspace-ok\n"
                    break
                except URLError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.2)
            result = container.exec_run(
                [
                    "sh",
                    "-ec",
                    'cat /workspace/marker /config/marker; echo "$VP_SNAP_SMOKE"; '
                    'printf "snap-write-ok\\n" > /workspace/container-output',
                ],
            )
            assert result.exit_code == 0
            assert b"snap-workspace-ok" in result.output
            assert b"snap-config-ok" in result.output
            assert b"snap-env-ok" in result.output
            assert (workspace / "container-output").read_text() == "snap-write-ok\n"
            assert container.id in {c.id for c in manager.list_managed(all_containers=True)}
            listed = subprocess.run(
                ["snap", "run", "vibepod", "list", "--running", "--json"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert container.name in {
                row["container"] for row in json.loads(listed.stdout)["running"]
            }
            subprocess.run(
                ["snap", "run", "vibepod", "stop", "--force", container.name],
                check=True,
                timeout=30,
            )
            container.reload()
            assert container.status == "exited"
        finally:
            container.remove(force=True)


if __name__ == "__main__":
    main()
