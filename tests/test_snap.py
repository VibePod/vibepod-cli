"""Validate Snapcraft's dynamic version extraction against project metadata."""

from __future__ import annotations

import os
import runpy
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
import yaml

from vibepod.core.docker import DockerManager

ROOT = Path(__file__).resolve().parents[1]


def _smoke(name: str) -> Any:
    return runpy.run_path(str(ROOT / "scripts/smoke_snap.py"))[name]


def _run_lifecycle_step(
    step: str,
    tmp_path: Path,
    extra_env: dict[str, str] | None = None,
) -> Path:
    """Execute the recipe's override script with craftctl stubbed out.

    Returns the file recording the craftctl calls.
    """
    if sys.platform == "win32" or sys.version_info < (3, 11):
        pytest.skip("Snapcraft builds on Linux with Python 3.12")
    recipe = yaml.safe_load((ROOT / "snap/snapcraft.yaml").read_text())
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "python3").symlink_to(sys.executable)
    calls = tmp_path / "calls"
    craftctl = stubs / "craftctl"
    craftctl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CRAFTCTL_CALLS"\n')
    craftctl.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "CRAFTCTL_CALLS": str(calls),
        **(extra_env or {}),
    }
    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", recipe["parts"][recipe["adopt-info"]][step]],
        cwd=ROOT,
        env=env,
        check=True,
    )
    return calls


def test_snap_adopts_project_version(tmp_path: Path) -> None:
    calls = _run_lifecycle_step("override-pull", tmp_path)
    import tomllib

    with (ROOT / "pyproject.toml").open("rb") as source:
        expected = tomllib.load(source)["project"]["version"]
    assert calls.read_text().splitlines() == ["default", f"set version={expected}"]


@pytest.mark.parametrize(
    ("expected", "podman", "rootless", "error"),
    [
        ("docker", False, False, None),
        ("rootless-podman", True, True, None),
        ("docker", True, True, "Expected Docker"),
        ("rootless-podman", False, False, "connected to another engine"),
        ("rootless-podman", True, False, "connected to rootful Podman"),
    ],
)
def test_snap_smoke_requires_expected_runtime(
    expected: str,
    podman: bool,
    rootless: bool,
    error: str | None,
) -> None:
    validate = _smoke("validate_runtime")
    manager = Mock(spec=DockerManager)
    manager.is_podman.return_value = podman
    manager.is_rootless_podman.return_value = rootless
    if error:
        with pytest.raises(AssertionError, match=error):
            validate(manager, expected)
    else:
        validate(manager, expected)


def test_snap_smoke_rejects_unknown_runtime() -> None:
    validate = _smoke("validate_runtime")
    with pytest.raises(ValueError, match="Unsupported expected runtime"):
        validate(Mock(spec=DockerManager), "podman")


def _fake_venv(install: Path) -> None:
    (install / "bin").mkdir(parents=True)
    (install / "pyvenv.cfg").write_text(
        "home = /usr/bin\ninclude-system-site-packages = false\nversion = 3.12.3\n",
    )
    for app in ("vibepod", "vp"):
        (install / "bin" / app).write_text(
            "#!/usr/bin/env python3\nimport sys\nfrom vibepod.cli import app\n",
        )


def test_snap_pins_apps_and_stdlib_to_bundled_python(tmp_path: Path) -> None:
    install = tmp_path / "install"
    _fake_venv(install)
    calls = _run_lifecycle_step(
        "override-build",
        tmp_path,
        {"CRAFT_PART_INSTALL": str(install), "CRAFT_PROJECT_NAME": "vibepod"},
    )
    assert calls.read_text().splitlines() == ["default"]
    pyvenv = (install / "pyvenv.cfg").read_text().splitlines()
    # A host-relative home makes the bundled interpreter load the host's stdlib.
    assert pyvenv[0] == "home = /snap/vibepod/current/usr/bin"
    assert pyvenv[1:] == ["include-system-site-packages = false", "version = 3.12.3"]
    interpreter = _smoke("script_interpreter")
    for app in ("vibepod", "vp"):
        script = install / "bin" / app
        assert interpreter(script) == Path("/snap/vibepod/current/bin/python3")
        assert script.read_text().splitlines()[1:] == ["import sys", "from vibepod.cli import app"]


def test_snap_build_fails_when_plugin_shebang_changes(tmp_path: Path) -> None:
    install = tmp_path / "install"
    _fake_venv(install)
    (install / "bin" / "vp").write_text("#!/usr/bin/python3\n")
    with pytest.raises(subprocess.CalledProcessError):
        _run_lifecycle_step(
            "override-build",
            tmp_path,
            {"CRAFT_PART_INSTALL": str(install), "CRAFT_PROJECT_NAME": "vibepod"},
        )


def test_snap_smoke_rejects_path_resolved_interpreter(tmp_path: Path) -> None:
    script = tmp_path / "vibepod"
    script.write_text("#!/usr/bin/env python3\n")
    with pytest.raises(AssertionError, match="must name its interpreter directly"):
        _smoke("script_interpreter")(script)


def test_snap_smoke_cli_path_excludes_runner_python() -> None:
    env = _smoke("snap_cli_env")()
    assert str(Path(sys.executable).parent) not in env["PATH"].split(os.pathsep)


@pytest.fixture()
def flaky_http_server() -> Iterator[tuple[str, list[int]]]:
    """Serve like a published port whose container is still starting.

    Docker's userland proxy accepts connections before the container listens and
    then closes them, which urllib reports as a reset rather than a URLError.
    """
    if sys.platform == "win32":
        pytest.skip("POSIX socket semantics")
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    accepted = [0]

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                accepted[0] += 1
                if accepted[0] <= 2:
                    continue
                conn.recv(4096)
                body = b"snap-workspace-ok\n"
                conn.sendall(
                    b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body),
                )

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.getsockname()[1]}/marker", accepted
    finally:
        server.close()


def test_snap_smoke_retries_connections_closed_before_response(
    flaky_http_server: tuple[str, list[int]],
) -> None:
    url, accepted = flaky_http_server
    _smoke("wait_for_http")(url, b"snap-workspace-ok\n", timeout=10)
    assert accepted[0] == 3


def test_snap_smoke_http_wait_reports_last_error() -> None:
    def refused(url: str, timeout: float) -> Any:
        raise ConnectionRefusedError("refused")

    with pytest.raises(AssertionError, match="ConnectionRefusedError"):
        _smoke("wait_for_http")("http://127.0.0.1:1/marker", b"ok", timeout=0, opener=refused)
