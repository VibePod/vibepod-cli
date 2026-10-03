"""The gVisor agent sandbox against a real engine with runsc registered.

Deselected by default; run with: pytest -m integration
Skips unless the engine is reachable and passes `vp doctor sandbox` checks
(runsc registered on rootful Docker with --network=host, or rootful Podman).
Set VP_GVISOR_RUNTIME to test a runtime registered under another name.
"""

from __future__ import annotations

import os
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from vibepod.core.docker import DockerClientError, DockerManager
from vibepod.core.sandbox import SandboxError, SandboxPlan, SandboxSettings, prepare_sandbox

pytestmark = pytest.mark.integration

SMOKE_IMAGE = "alpine:3.20"
SMOKE_AGENT = "integration-gvisor"


@pytest.fixture()
def manager() -> DockerManager:
    try:
        return DockerManager()
    except DockerClientError as exc:
        pytest.skip(f"Container runtime not reachable: {exc}")


@pytest.fixture()
def plan(manager: DockerManager) -> SandboxPlan:
    runtime = os.environ.get("VP_GVISOR_RUNTIME", "runsc")
    try:
        return prepare_sandbox(manager, SandboxSettings(mode="gvisor", runtime=runtime))
    except SandboxError as exc:
        pytest.skip(f"gVisor sandbox unavailable: {exc}")


@pytest.fixture()
def workspace() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def _run(
    manager: DockerManager,
    plan: SandboxPlan,
    workspace: Path,
    script: str,
    network: str | None = None,
) -> str:
    if manager.image_id(SMOKE_IMAGE) is None:
        manager.pull_image(SMOKE_IMAGE)
    container = manager.run_agent(
        agent=SMOKE_AGENT,
        image=SMOKE_IMAGE,
        workspace=workspace,
        config_dir=workspace,
        config_mount_path="/config",
        env={},
        command=["sh", "-c", script],
        auto_remove=False,
        name=f"vibepod-{SMOKE_AGENT}-{uuid.uuid4().hex[:8]}",
        version="integration",
        network=network,
        tty=False,
        runtime=plan.runtime,
        security_opt=plan.security_opt or None,
        extra_labels=plan.labels,
    )
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            container.reload()
            if container.status in {"exited", "dead"}:
                break
            time.sleep(0.5)
        assert container.attrs["HostConfig"]["Runtime"] == plan.runtime
        return container.logs().decode("utf-8", errors="replace")
    finally:
        container.remove(force=True)


def test_agent_container_runs_inside_gvisor(
    manager: DockerManager,
    plan: SandboxPlan,
    workspace: Path,
) -> None:
    (workspace / "from-host.txt").write_text("host\n", encoding="utf-8")

    output = _run(
        manager,
        plan,
        workspace,
        "dmesg | head -n 1; cat /workspace/from-host.txt; echo agent > /workspace/from-agent.txt",
    )

    # Every gVisor boot log starts with this line; runc has no such dmesg.
    assert "Starting gVisor" in output
    assert "host" in output
    # The workspace bind mount stays writable and visible on the host.
    assert (workspace / "from-agent.txt").read_text(encoding="utf-8") == "agent\n"


def test_sandboxed_agent_resolves_names_on_the_vibepod_network(
    manager: DockerManager,
    plan: SandboxPlan,
    workspace: Path,
) -> None:
    # VibePod always attaches agents to a user-defined network; Docker serves
    # DNS there on 127.0.0.11, which gVisor's netstack cannot reach.
    network = f"vibepod-gvisor-{uuid.uuid4().hex[:6]}"
    manager.ensure_network(network)
    try:
        output = _run(
            manager,
            plan,
            workspace,
            "nslookup example.com >/dev/null && echo resolved",
            network=network,
        )
    finally:
        manager.client.networks.get(network).remove()

    assert "resolved" in output
