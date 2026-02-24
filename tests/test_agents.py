"""Agent registry tests."""

from __future__ import annotations

import pytest

from vibepod.core.agents import get_agent_spec, is_supported_agent


def test_supported_agent() -> None:
    assert is_supported_agent("claude") is True
    assert is_supported_agent("copilot") is True
    assert is_supported_agent("codex") is True
    assert is_supported_agent("unknown") is False


def test_get_agent_spec_supported() -> None:
    assert get_agent_spec("copilot").id == "copilot"
    assert get_agent_spec("codex").id == "codex"


def test_devstral_spec_matches_container_contract() -> None:
    spec = get_agent_spec("devstral")
    assert spec.command is None
    assert spec.extra_env["HOME"] == "/config"
    assert spec.extra_env["WORKSPACE_PATH"] == "/workspace"
    assert spec.platform == "linux/amd64"
    assert spec.run_as_host_user is True


def test_get_agent_spec_unknown() -> None:
    with pytest.raises(ValueError):
        get_agent_spec("unknown")
