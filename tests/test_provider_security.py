"""Adversarial input tests for provider metadata and credentials."""

import os

import pytest

from vibepod.core.provider_discovery import discover_models
from vibepod.core.providers import Provider, load_provider, save_provider

# The provider store enforces POSIX owner-only permissions and refuses other platforms.
pytestmark = pytest.mark.skipif(os.name == "nt", reason="provider store is POSIX-only")


@pytest.mark.parametrize("key", ["secret\r\nInjected: value", "secret\n", "secret\x00"])
def test_discovery_rejects_header_controls_without_echoing_key(key):
    p = Provider("test", "openai-chat", "https://example.invalid/v1")
    with pytest.raises(ValueError, match="key") as caught:
        discover_models(p, key=key)
    assert "secret" not in str(caught.value)


def test_metadata_rejects_terminal_control_model_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("VP_PROVIDERS_DIR", str(tmp_path / "providers"))
    with pytest.raises(ValueError, match="Model"):
        save_provider(
            Provider("local", "openai-chat", "http://localhost", models=("model\x1b[2J",)),
        )


def test_malformed_scalar_metadata_has_safe_error(tmp_path, monkeypatch):
    root = tmp_path / "providers"
    monkeypatch.setenv("VP_PROVIDERS_DIR", str(root))
    save_provider(Provider("local", "openai-chat", "http://localhost"))
    path = root / "local/provider.toml"
    path.write_text('version = 1\nname = "local"\nprotocol = "openai-chat"\nbase_url = 123\n')
    with pytest.raises(ValueError, match="Invalid metadata"):
        load_provider("local")
