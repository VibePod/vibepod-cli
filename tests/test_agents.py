"""Agent registry tests."""

from __future__ import annotations

import pytest

from vibepod.constants import AGENT_SHORTCUTS, SUPPORTED_AGENTS
from vibepod.core.agents import (
    get_agent_shortcut,
    get_agent_spec,
    is_supported_agent,
    resolve_agent_name,
)


def test_supported_agent() -> None:
    for agent in SUPPORTED_AGENTS:
        assert is_supported_agent(agent) is True
    assert is_supported_agent("unknown") is False


def test_get_agent_spec_supported() -> None:
    for agent in SUPPORTED_AGENTS:
        spec = get_agent_spec(agent)
        assert spec.id == agent
        assert spec.provider
        assert spec.image
        assert spec.config_subdir
        assert spec.config_mount_path
        assert isinstance(spec.extra_env, dict)


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


def test_resolve_agent_name_accepts_short_and_full_forms() -> None:
    for shortcut, agent in AGENT_SHORTCUTS.items():
        assert resolve_agent_name(shortcut) == agent
        assert resolve_agent_name(shortcut.upper()) == agent
    for agent in SUPPORTED_AGENTS:
        assert resolve_agent_name(agent) == agent
        assert resolve_agent_name(f" {agent.upper()} ") == agent
    assert resolve_agent_name("vibe") == "devstral"
    assert resolve_agent_name("VIBE") == "devstral"
    assert resolve_agent_name("unknown") is None


def test_claude_spec_has_ikwid_args() -> None:
    spec = get_agent_spec("claude")
    assert spec.ikwid_args == ["--dangerously-skip-permissions"]


def test_codex_spec_has_ikwid_args() -> None:
    spec = get_agent_spec("codex")
    assert spec.ikwid_args == ["--dangerously-bypass-approvals-and-sandbox"]


def test_gemini_spec_has_ikwid_args() -> None:
    spec = get_agent_spec("gemini")
    assert spec.ikwid_args == ["--approval-mode=yolo"]


def test_copilot_spec_has_ikwid_args() -> None:
    spec = get_agent_spec("copilot")
    assert spec.ikwid_args == ["--yolo"]


def test_devstral_spec_has_ikwid_args() -> None:
    spec = get_agent_spec("devstral")
    assert spec.ikwid_args == ["--auto-approve"]


def test_gemini_spec_runs_via_node_wrapper() -> None:
    spec = get_agent_spec("gemini")
    assert spec.command == ["env", "HOME=/config", "node", "/usr/local/bin/gemini"]


def test_unsupported_agents_have_no_ikwid_args() -> None:
    for agent in ("opencode", "auggie"):
        spec = get_agent_spec(agent)
        assert spec.ikwid_args is None, f"{agent} should not have ikwid_args"


def test_get_agent_shortcut_known_agent() -> None:
    expected_by_agent = {agent: shortcut for shortcut, agent in AGENT_SHORTCUTS.items()}
    assert set(expected_by_agent.keys()) == set(SUPPORTED_AGENTS)
    for agent in SUPPORTED_AGENTS:
        assert get_agent_shortcut(agent) == expected_by_agent[agent]
        assert get_agent_shortcut(f" {agent.upper()} ") == expected_by_agent[agent]
    assert get_agent_shortcut("unknown") is None


def test_claude_spec_has_llm_env_map() -> None:
    spec = get_agent_spec("claude")
    assert spec.llm_env_map == {
        "base_url": "ANTHROPIC_BASE_URL",
        "api_key": ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"],
        "model": [
            "ANTHROPIC_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        ],
    }
    assert spec.llm_model_args == ["--model"]


def test_codex_spec_has_llm_env_map() -> None:
    spec = get_agent_spec("codex")
    assert spec.llm_env_map == {
        "base_url": "CODEX_OSS_BASE_URL",
    }
    assert spec.llm_model_args == ["--oss", "-m"]


def test_agents_without_llm_env_map() -> None:
    for agent in ("gemini", "opencode", "devstral", "auggie", "copilot"):
        spec = get_agent_spec(agent)
        assert spec.llm_env_map is None, f"{agent} should not have llm_env_map"
