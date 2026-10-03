"""Temporary provider routing must not modify profile files."""

import os

import pytest

from vibepod.core import provider_launch
from vibepod.core.providers import Provider, save_provider

# The provider store enforces POSIX owner-only permissions and refuses other platforms.
pytestmark = pytest.mark.skipif(os.name == "nt", reason="provider store is POSIX-only")


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("VP_PROVIDERS_DIR", str(tmp_path / "providers"))
    save_provider(
        Provider(
            "hosted",
            "anthropic",
            "https://example.com",
            auth="key",
            models=("model-a",),
            default_model="model-a",
        ),
        key="private-key",
    )
    return tmp_path


def test_explicit_model_drops_every_default_model_argument():
    from vibepod.core.provider_launch import ProviderLaunch

    launch = ProviderLaunch({}, ["--provider", "x"], ["--model", "m", "--thinking", "high"])
    assert launch.arguments([]) == ["--provider", "x", "--model", "m", "--thinking", "high"]
    for override in (["-m", "y"], ["--model=y"], ["--model", "y"]):
        assert launch.arguments(override) == ["--provider", "x"]


def test_qwen_environment_routing(registry):
    save_provider(
        Provider(
            "chat",
            "openai-chat",
            "http://localhost:11434/v1",
            models=("m",),
            default_model="m",
        ),
    )
    env, args = provider_launch.prepare_provider("qwen", ["chat"], {})
    assert env == {
        "OPENAI_BASE_URL": "http://localhost:11434/v1",
        "OPENAI_API_KEY": "vibepod-local",
        "OPENAI_MODEL": "m",
    }
    assert args == []
    with pytest.raises(ValueError, match="OPENAI_MODEL"):
        provider_launch.prepare_provider("qwen", ["chat"], {"OPENAI_MODEL": "x"})
    with pytest.raises(ValueError, match="Qwen requires"):
        provider_launch.prepare_provider("qwen", ["chat", "hosted"], {})


def test_claude_provider_environment(registry):
    env, args = provider_launch.prepare_provider("claude", ["hosted"], {})
    assert env["ANTHROPIC_BASE_URL"] == "https://example.com"
    assert env["ANTHROPIC_API_KEY"] == "private-key"
    assert env["ANTHROPIC_MODEL"] == "model-a"
    assert args == ["--model", "model-a"]
    assert not (registry / "agents").exists()


def test_unsupported_agent_rejected_before_key_read(registry, monkeypatch):
    def unexpected(*args):
        raise AssertionError("Credentials must not be read for unsupported adapters")

    monkeypatch.setattr(provider_launch, "resolve_key", unexpected)
    with pytest.raises(ValueError, match="not yet supported"):
        provider_launch.prepare_provider("gemini", ["hosted"], {})


def test_conflicting_route_rejected_without_values(registry):
    with pytest.raises(ValueError, match="ANTHROPIC_BASE_URL") as exc:
        provider_launch.prepare_provider(
            "claude",
            ["hosted"],
            {"ANTHROPIC_BASE_URL": "secret-host"},
        )
    assert "secret-host" not in str(exc.value)


def test_protocol_and_multiple_provider_rejection(registry):
    save_provider(Provider("local", "openai-chat", "http://localhost/v1"))
    with pytest.raises(ValueError, match="Anthropic"):
        provider_launch.prepare_provider("claude", ["local"], {})
    with pytest.raises(ValueError, match="one provider"):
        provider_launch.prepare_provider("claude", ["hosted", "local"], {})


def test_local_gateway_url_and_no_auth(registry):
    save_provider(
        Provider(
            "local",
            "anthropic",
            "http://host.docker.internal:11434",
            models=("m",),
            default_model="m",
        ),
    )
    env, _ = provider_launch.prepare_provider("claude", ["local"], {})
    assert env["ANTHROPIC_BASE_URL"] == "http://host.docker.internal:11434"
    assert env["ANTHROPIC_API_KEY"]


def test_authenticated_http_runtime_rejected(registry):
    save_provider(
        Provider(
            "insecure",
            "anthropic",
            "http://example.com",
            auth="key",
            models=("m",),
            default_model="m",
        ),
        key="secret",
    )
    with pytest.raises(ValueError, match="HTTPS"):
        provider_launch.prepare_provider("claude", ["insecure"], {})


def test_default_model_required_for_claude(registry):
    save_provider(Provider("local", "anthropic", "http://localhost:11434", models=("m",)))
    with pytest.raises(ValueError, match="default model"):
        provider_launch.prepare_provider("claude", ["local"], {})


def test_claude_receives_max_output_tokens_for_the_launch_model(registry):
    from dataclasses import replace

    from vibepod.core.providers import ModelSettings, load_provider, update_provider

    hosted = load_provider("hosted")
    update_provider(
        replace(hosted, model_settings={"model-a": ModelSettings(max_output_tokens=4096)}),
    )
    env, _ = provider_launch.prepare_provider("claude", ["hosted"], {})
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "4096"
    with pytest.raises(ValueError, match="CLAUDE_CODE_MAX_OUTPUT_TOKENS"):
        provider_launch.prepare_provider(
            "claude",
            ["hosted"],
            {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1"},
        )
