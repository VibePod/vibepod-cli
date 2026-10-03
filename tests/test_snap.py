"""Validate Snapcraft's dynamic version extraction against project metadata."""

from __future__ import annotations

import os
import runpy
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from vibepod.core.docker import DockerManager

ROOT = Path(__file__).resolve().parents[1]


def test_snap_adopts_project_version(tmp_path: Path) -> None:
    if sys.platform == "win32" or sys.version_info < (3, 11):
        pytest.skip("Snapcraft builds on Linux with Python 3.12")
    recipe = yaml.safe_load((ROOT / "snap/snapcraft.yaml").read_text())
    # Stub craftctl so the actual lifecycle script can execute without snapd.
    (tmp_path / "python3").symlink_to(sys.executable)
    calls = tmp_path / "calls"
    craftctl = tmp_path / "craftctl"
    craftctl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CRAFTCTL_CALLS"\n')
    craftctl.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "CRAFTCTL_CALLS": str(calls)}
    subprocess.run(
        ["sh", "-eu", "-c", recipe["parts"][recipe["adopt-info"]]["override-pull"]],
        cwd=ROOT,
        env=env,
        check=True,
    )
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
    expected: str, podman: bool, rootless: bool, error: str | None
) -> None:
    validate = runpy.run_path(str(ROOT / "scripts/smoke_snap.py"))["validate_runtime"]
    manager = Mock(spec=DockerManager)
    manager.is_podman.return_value = podman
    manager.is_rootless_podman.return_value = rootless
    if error:
        with pytest.raises(AssertionError, match=error):
            validate(manager, expected)
    else:
        validate(manager, expected)


def test_snap_smoke_rejects_unknown_runtime() -> None:
    validate = runpy.run_path(str(ROOT / "scripts/smoke_snap.py"))["validate_runtime"]
    with pytest.raises(ValueError, match="Unsupported expected runtime"):
        validate(Mock(spec=DockerManager), "podman")
