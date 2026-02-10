"""Agent registry tests."""

from __future__ import annotations

import pytest

from vibepod.core.agents import get_agent_spec, is_supported_agent


def test_supported_agent() -> None:
    assert is_supported_agent("claude") is True
    assert is_supported_agent("unknown") is False


def test_get_agent_spec_unknown() -> None:
    with pytest.raises(ValueError):
        get_agent_spec("unknown")
