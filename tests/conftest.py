"""Test configuration for local src-layout imports."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def _reset_console_routing():
    """Undo `route_to_stderr()` (flipped by `vp run --acp`) after each test.

    The console is one shared module object; without this, stdout assertions
    in tests that run after an ACP test become order-dependent.
    """
    from vibepod.utils import console as console_mod

    console = console_mod.console
    was_stderr = console.stderr
    yield
    console_mod.console = console
    console.stderr = was_stderr


@pytest.fixture(autouse=True)
def _no_selinux_relabel(monkeypatch):
    """Pin bind modes to the plain `rw`/`ro` form so bind assertions are hermetic.

    On an enforcing SELinux host every bind would otherwise gain a `,z`
    suffix, which would make the suite pass or fail by host policy.
    `test_docker.py` points the probe at a real file to cover that path.
    """
    from vibepod.core import docker as docker_mod

    monkeypatch.setattr(docker_mod, "_SELINUX_ENFORCE_PATH", "/nonexistent/selinux/enforce")


@pytest.fixture(autouse=True)
def _no_ambient_herdr_env(monkeypatch):
    """Strip herdr pane env so tests are hermetic.

    Running the suite inside a herdr pane (e.g. an agent container started
    by `vp run` in herdr) would otherwise activate the wiring — including
    the HERDR_AGENT re-exec, which would exec pytest itself.
    """
    for key in list(os.environ):
        if key.startswith("HERDR_"):
            monkeypatch.delenv(key, raising=False)
