"""syd sandbox inside a real container (Docker or Podman).

Deselected by default; run with a static syd binary:
VP_TEST_SYD_BINARY=/path/to/syd pytest -m integration tests/integration/test_sandbox_smoke.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from vibepod.core.docker import DockerClientError, DockerManager
from vibepod.core.sandbox import (
    check_syd_prerequisites,
    prepare_syd_sandbox,
    wrap_command,
)

SMOKE_IMAGE = "alpine:3.20"
SMOKE_AGENT = "integration-sandbox"
_SYD_BINARY = os.environ.get("VP_TEST_SYD_BINARY", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _SYD_BINARY, reason="set VP_TEST_SYD_BINARY to a static syd binary"),
]


@pytest.fixture()
def manager() -> DockerManager:
    try:
        return DockerManager()
    except DockerClientError as exc:
        pytest.fail(f"Container runtime not reachable: {exc}")


@pytest.fixture()
def mountable_tmp_path() -> Iterator[Path]:
    base: Path | None = None
    if sys.platform == "darwin":
        base = Path.home() / ".vibepod-integration-tmp"
        base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=base) as tmp:
        yield Path(tmp)


def test_syd_confines_writes_inside_container(
    manager: DockerManager,
    mountable_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VP_CONFIG_DIR", str(mountable_tmp_path / "vpcfg"))
    workspace = mountable_tmp_path / "workspace"
    config_dir = mountable_tmp_path / "config"
    (workspace / ".git" / "hooks").mkdir(parents=True)
    config_dir.mkdir()

    prerequisites = check_syd_prerequisites({"sandbox": {"syd": {"binary": _SYD_BINARY}}})
    syd = prepare_syd_sandbox(
        prerequisites,
        agent=SMOKE_AGENT,
        workspace_paths=["/workspace"],
        writable_paths=["/config"],
    )
    script = (
        "cd /workspace; "
        "echo ok > f && echo ws-ok; "
        "echo x > .git/hooks/pre-commit 2>/dev/null && echo HOOK || echo hook-denied; "
        "echo x > /etc/vp 2>/dev/null && echo ETC || echo etc-denied; "
        'echo "env=$VIBEPOD_SMOKE"; echo sandbox-done'
    )
    manager.pull_image(SMOKE_IMAGE)
    container = manager.run_agent(
        agent=SMOKE_AGENT,
        image=SMOKE_IMAGE,
        workspace=workspace,
        config_dir=config_dir,
        config_mount_path="/config",
        env={"VIBEPOD_SMOKE": "kept"},
        command=wrap_command(["sh", "-c", script], 0, syd),
        auto_remove=False,
        name=f"vibepod-{SMOKE_AGENT}-{uuid.uuid4().hex[:8]}",
        version="integration-test",
        extra_volumes=syd.volumes(),
    )
    try:
        deadline = time.monotonic() + 60
        logs = b""
        while time.monotonic() < deadline:
            container.reload()
            logs = container.logs()
            if b"sandbox-done" in logs or container.status in ("exited", "dead"):
                break
            time.sleep(0.5)
        text = logs.decode("utf-8", errors="replace")
        for marker in ("ws-ok", "hook-denied", "etc-denied", "env=kept", "sandbox-done"):
            assert marker in text, text
        assert (workspace / "f").read_text() == "ok\n"
        assert not (workspace / ".git" / "hooks" / "pre-commit").exists()
    finally:
        container.remove(force=True)
