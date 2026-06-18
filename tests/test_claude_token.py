"""Tests for the claude long-lived token storage + injection."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from vibepod.commands import run as run_cmd


def test_read_missing_token(tmp_path: Path) -> None:
    assert run_cmd._read_claude_stored_token(tmp_path) is None


def test_write_and_read_token(tmp_path: Path) -> None:
    path = run_cmd._write_claude_stored_token(tmp_path, "  sk-abc123  \n")
    assert path == tmp_path / "oauth-token"
    assert path.read_text(encoding="utf-8") == "sk-abc123\n"
    if os.name == "posix" and hasattr(os, "fchmod"):
        assert oct(path.stat().st_mode)[-3:] == "600"
    assert run_cmd._read_claude_stored_token(tmp_path) == "sk-abc123"


def test_empty_token_file_is_none(tmp_path: Path) -> None:
    (tmp_path / "oauth-token").write_text("   \n", encoding="utf-8")
    assert run_cmd._read_claude_stored_token(tmp_path) is None


def test_token_filename_matches_doctor(tmp_path: Path) -> None:
    """The filename written by run.py must match what doctor.py inspects."""
    path = run_cmd._write_claude_stored_token(tmp_path, "sk-abc")
    # doctor.py reads `<config_dir>/oauth-token` directly
    assert path.name == "oauth-token"


def test_write_token_works_without_fchmod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(run_cmd.os, "fchmod", raising=False)

    path = run_cmd._write_claude_stored_token(tmp_path, "sk-windows")

    assert path.read_text(encoding="utf-8") == "sk-windows\n"


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "fchmod"),
    reason="POSIX fchmod permissions not available",
)
def test_write_token_permissions_ignore_umask(tmp_path: Path) -> None:
    old_umask = os.umask(0o177)
    try:
        path = run_cmd._write_claude_stored_token(tmp_path, "sk-umask-test")
    finally:
        os.umask(old_umask)
    assert oct(path.stat().st_mode)[-3:] == "600"
