"""Real-runtime integration tests for `vp version`.

These tests are intended for dedicated CI jobs that prepare concrete host
runtime scenarios. They are skipped unless ``VP_REAL_RUNTIME_SCENARIO`` is set.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCENARIO = os.environ.get("VP_REAL_RUNTIME_SCENARIO")

if SCENARIO is None:
    pytest.skip(
        "requires dedicated runtime integration CI",
        allow_module_level=True,
    )

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def _run_vp(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["VP_CONFIG_DIR"] = str(tmp_path / "global-config")
    env["PYTHONPATH"] = (
        f"{SRC}{os.pathsep}{env['PYTHONPATH']}"
        if "PYTHONPATH" in env and env["PYTHONPATH"]
        else str(SRC)
    )
    env.pop("VP_CONTAINER_RUNTIME", None)
    return subprocess.run(
        [sys.executable, "-m", "vibepod.cli", *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=60,
    )


def _assert_ok(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, (
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def _runtime_parts(output: str) -> tuple[str, str]:
    for line in output.splitlines():
        if not line.startswith("Runtime:"):
            continue
        _, runtime_name, runtime_version = line.split(maxsplit=2)
        return runtime_name, runtime_version
    raise AssertionError(f"Missing Runtime line in output:\n{output}")


def _assert_runtime(
    result: subprocess.CompletedProcess[str],
    *,
    runtime_name: str,
) -> None:
    _assert_ok(result)
    assert "VibePod CLI:" in result.stdout
    assert "Python:" in result.stdout
    actual_runtime, actual_version = _runtime_parts(result.stdout)
    assert actual_runtime == runtime_name
    assert actual_version not in {"unknown", "unavailable"}


def _assert_unavailable(result: subprocess.CompletedProcess[str]) -> None:
    _assert_ok(result)
    assert _runtime_parts(result.stdout) == ("unknown", "unavailable")


def test_version_with_docker_only(tmp_path: Path) -> None:
    if SCENARIO != "docker-only":
        pytest.skip("scenario mismatch")

    result = _run_vp(tmp_path, "version")

    _assert_runtime(result, runtime_name="docker")


def test_version_with_podman_only(tmp_path: Path) -> None:
    if SCENARIO != "podman-only":
        pytest.skip("scenario mismatch")

    result = _run_vp(tmp_path, "version")

    _assert_runtime(result, runtime_name="podman")


def test_version_with_no_runtime_available(tmp_path: Path) -> None:
    if SCENARIO != "none":
        pytest.skip("scenario mismatch")

    result = _run_vp(tmp_path, "version")

    _assert_unavailable(result)


def test_version_with_both_runtimes_defaults_to_docker_non_interactive(
    tmp_path: Path,
) -> None:
    if SCENARIO != "both-auto":
        pytest.skip("scenario mismatch")

    result = _run_vp(tmp_path, "version")

    _assert_runtime(result, runtime_name="docker")


def test_version_uses_saved_docker_default_when_both_runtimes_are_available(
    tmp_path: Path,
) -> None:
    if SCENARIO != "both-default-docker":
        pytest.skip("scenario mismatch")

    set_result = _run_vp(tmp_path, "config", "runtime", "docker")
    result = _run_vp(tmp_path, "version")

    _assert_ok(set_result)
    assert "Set default container runtime to 'docker'" in set_result.stdout
    _assert_runtime(result, runtime_name="docker")


def test_version_uses_saved_podman_default_when_both_runtimes_are_available(
    tmp_path: Path,
) -> None:
    if SCENARIO != "both-default-podman":
        pytest.skip("scenario mismatch")

    set_result = _run_vp(tmp_path, "config", "runtime", "podman")
    result = _run_vp(tmp_path, "version")

    _assert_ok(set_result)
    assert "Set default container runtime to 'podman'" in set_result.stdout
    _assert_runtime(result, runtime_name="podman")


def test_version_reflects_runtime_switch_command(tmp_path: Path) -> None:
    if SCENARIO != "both-switch":
        pytest.skip("scenario mismatch")

    first_set_result = _run_vp(tmp_path, "config", "runtime", "docker")
    first_version_result = _run_vp(tmp_path, "version")
    second_set_result = _run_vp(tmp_path, "config", "runtime", "podman")
    second_version_result = _run_vp(tmp_path, "version")

    _assert_ok(first_set_result)
    _assert_runtime(first_version_result, runtime_name="docker")
    _assert_ok(second_set_result)
    _assert_runtime(second_version_result, runtime_name="podman")
