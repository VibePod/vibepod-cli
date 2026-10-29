"""Run with the installed snap's Python on a host with a container daemon."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from http.client import HTTPException
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.request import urlopen
from uuid import uuid4

from vibepod.core.docker import DockerManager


def validate_runtime(manager: DockerManager, expected: str) -> None:
    """Reject a fallback daemon before claiming runtime support."""
    if expected == "docker":
        assert not manager.is_podman(), "Expected Docker, connected to Podman"
    elif expected == "rootless-podman":
        assert manager.is_podman(), "Expected rootless Podman, connected to another engine"
        assert manager.is_rootless_podman(), "Expected rootless Podman, connected to rootful Podman"
    else:
        raise ValueError(f"Unsupported expected runtime: {expected!r}")


def script_interpreter(script: Path) -> Path:
    """Return the interpreter named by a script's shebang line."""
    first_line = script.read_text(encoding="utf-8", errors="replace").partition("\n")[0]
    assert first_line.startswith("#!"), f"{script} has no shebang: {first_line!r}"
    parts = first_line[2:].split()
    assert len(parts) == 1, f"{script} must name its interpreter directly: {first_line!r}"
    return Path(parts[0])


def validate_bundled_python(snap_root: Path) -> None:
    """Fail when the snap's Python, stdlib or apps resolve to the host's Python."""
    assert Path(sys.executable).resolve().is_relative_to(snap_root), sys.executable
    assert Path(os.__file__).resolve().is_relative_to(snap_root), (
        f"stdlib loaded from the host: {os.__file__}"
    )
    import vibepod

    assert Path(vibepod.__file__).resolve().is_relative_to(snap_root), vibepod.__file__
    for app in ("vibepod", "vp"):
        interpreter = script_interpreter(snap_root / "bin" / app)
        assert interpreter.resolve().is_relative_to(snap_root), (
            f"bin/{app} runs {interpreter}, outside {snap_root}"
        )


def snap_cli_env() -> dict[str, str]:
    """Environment for nested `snap run` calls without the runner's Python on PATH.

    A host python3 with vibepod installed (such as the CI checkout) would otherwise
    hide a snap whose apps resolve python3 through PATH.
    """
    return {**os.environ, "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}


def run_snap_cli(command: str, *args: str) -> str:
    """Run an installed snap app and return its stdout, reporting stderr on failure."""
    completed = subprocess.run(
        ["snap", "run", command, *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=snap_cli_env(),
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"`snap run {command} {' '.join(args)}` exited {completed.returncode}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
    return completed.stdout


def wait_for_http(
    url: str,
    expected: bytes,
    timeout: float = 30.0,
    opener: Callable[..., Any] = urlopen,
) -> None:
    """Poll *url* until it serves *expected*.

    Published ports accept connections before the container process listens:
    Docker's userland proxy then closes them, surfacing as resets or a missing
    response rather than URLError, so every connection error is retried.
    """
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while True:
        try:
            with opener(url, timeout=2) as response:
                body = response.read()
            if body == expected:
                return
            last_error = AssertionError(f"unexpected body {body!r}")
        except (OSError, HTTPException) as exc:
            last_error = exc
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"{url} did not serve {expected!r} within {timeout}s: {last_error!r}",
            )
        time.sleep(0.2)


def main() -> None:
    snap_root = Path(os.environ["SNAP"]).resolve()
    expected_version = os.environ["VP_SNAP_EXPECTED_VERSION"]
    validate_bundled_python(snap_root)
    assert version("vibepod") == expected_version
    for command in ("vibepod", "vibepod.vp"):
        reported = run_snap_cli(command, "version")
        assert reported.splitlines()[0] == f"VibePod CLI: {expected_version}", reported
    run_snap_cli("vibepod", "--help")
    print("snap smoke: bundled Python and CLI entry points OK", flush=True)
    manager = DockerManager()
    validate_runtime(manager, os.environ["VP_SNAP_EXPECTED_RUNTIME"])
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
            try:
                wait_for_http(f"http://127.0.0.1:{port}/marker", b"snap-workspace-ok\n")
            except AssertionError:
                container.reload()
                print(f"container status: {container.status}", file=sys.stderr)
                print(container.logs().decode(errors="replace"), file=sys.stderr)
                raise
            print("snap smoke: published port OK", flush=True)
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
            listed = run_snap_cli("vibepod", "list", "--running", "--json")
            assert container.name in {row["container"] for row in json.loads(listed)["running"]}
            run_snap_cli("vibepod", "stop", "--force", container.name)
            container.reload()
            assert container.status == "exited"
        finally:
            container.remove(force=True)
    print("snap smoke: container lifecycle OK", flush=True)


if __name__ == "__main__":
    main()
