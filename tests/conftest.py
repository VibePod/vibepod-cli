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
def _no_ambient_herdr_env(monkeypatch):
    """Strip herdr pane env so tests are hermetic.

    Running the suite inside a herdr pane (e.g. an agent container started
    by `vp run` in herdr) would otherwise activate the wiring — including
    the HERDR_AGENT re-exec, which would exec pytest itself.
    """
    for key in list(os.environ):
        if key.startswith("HERDR_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _no_ambient_dash_env(monkeypatch):
    """Strip VPDASH_* env so tests never report to a real dashboard.

    A developer with VPDASH_URL exported (the zero-config way to point `vp run`
    at a board) would otherwise have the suite post events to it.
    """
    for key in list(os.environ):
        if key.startswith("VPDASH_"):
            monkeypatch.delenv(key, raising=False)
