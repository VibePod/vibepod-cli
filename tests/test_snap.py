"""Validate Snapcraft's dynamic version extraction against project metadata."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

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
