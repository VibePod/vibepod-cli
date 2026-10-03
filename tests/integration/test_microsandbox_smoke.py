"""Smoke tests against a real microsandbox runtime.

Deselected by default; run with: pytest -m integration
Skipped when the `msb` CLI or the host virtualization support (KVM on Linux,
Apple Silicon on macOS) is missing.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from vibepod.core import microsandbox as msb

pytestmark = pytest.mark.integration

SMOKE_IMAGE = "alpine:3.20"


@pytest.fixture()
def binary() -> str:
    settings = msb.MicrosandboxSettings()
    try:
        return msb.ensure_available(settings)
    except msb.MicrosandboxError as exc:
        pytest.skip(f"microsandbox not available: {exc}")


def test_msb_run_sees_workspace_env_and_workdir(binary: str, tmp_path: Path) -> None:
    marker = uuid.uuid4().hex
    (tmp_path / "marker.txt").write_text(marker, encoding="utf-8")
    conf = msb.write_runtime_conf(
        env={"VIBEPOD_SMOKE": marker},
        labels={"vibepod.managed": "true"},
        workdir="/workspace",
    )
    try:
        argv = msb.build_run_argv(
            binary=binary,
            settings=msb.MicrosandboxSettings(memory="256M"),
            image=SMOKE_IMAGE,
            runtime_conf=conf,
            volumes=[(str(tmp_path), "/workspace", "rw")],
            ports=None,
            command=[
                "sh",
                "-c",
                'pwd; cat marker.txt; echo; echo "$VIBEPOD_SMOKE"; echo written > out.txt',
            ],
            name=None,
            detach=False,
        )
        result = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    finally:
        conf.unlink(missing_ok=True)

    assert result.returncode == 0, result.stderr
    lines = result.stdout.split()
    assert lines[:3] == ["/workspace", marker, marker]
    assert (tmp_path / "out.txt").read_text(encoding="utf-8").strip() == "written"
