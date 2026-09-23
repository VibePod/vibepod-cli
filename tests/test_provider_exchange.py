"""Provider exchange files: credential-free export and import."""

import os

import pytest

from vibepod.core import providers
from vibepod.core.providers import ModelSettings, Provider

# The provider store enforces POSIX owner-only permissions and refuses other platforms.
pytestmark = pytest.mark.skipif(os.name == "nt", reason="provider store is POSIX-only")


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "providers"
    monkeypatch.setenv("VP_PROVIDERS_DIR", str(root))
    return root


def hosted():
    return Provider(
        "hosted",
        "openai-chat",
        "https://api.example.com/v1",
        auth="key",
        models=("a", "b"),
        default_model="b",
        model_settings={
            "b": ModelSettings(context_window=128000, reasoning_levels=("low", "high")),
        },
    )


def test_render_exchange_has_no_credentials(store):
    providers.save_provider(hosted(), key="sk-secret-value")
    text = providers.render_exchange(providers.load_provider("hosted"))
    assert text.startswith("# VibePod provider definition")
    assert "sk-secret-value" not in text
    assert "credential_file" not in text
    assert 'auth = "key"' in text and 'default_model = "b"' in text
    assert '[model_settings."b"]' in text and "context_window = 128000" in text


def test_provider_from_toml_round_trips_and_ignores_credential_reference(store):
    original = hosted()
    text = providers.render_exchange(original).replace(
        'auth = "key"',
        'credential_file = "credentials-0123456789abcdef0123456789abcdef.json"\nauth = "key"',
    )
    parsed = providers.provider_from_toml(text, name="copy")
    assert parsed.name == "copy"
    assert parsed.credential_file == "credentials.json"
    assert parsed.models == original.models
    assert parsed.model_settings == original.model_settings
    assert parsed.auth == "key"


@pytest.mark.parametrize(
    "text, message",
    [
        ('version = 2\nname = "x"\n', "version"),
        ("not toml [", "Invalid provider file"),
        (
            'version = 1\nname = "x"\nprotocol = "openai-chat"\n'
            'base_url = "https://e/v1"\nextra = 1\n',
            "Invalid provider file",
        ),
        ('version = 1\nname = "x"\nprotocol = "openai-chat"\nbase_url = "ftp://e"\n', "HTTP"),
    ],
)
def test_provider_from_toml_rejects_bad_files(text, message):
    with pytest.raises(ValueError, match=message):
        providers.provider_from_toml(text)
