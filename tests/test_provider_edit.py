"""Editing providers preserves keys unless explicitly replaced."""

import os
from dataclasses import replace

import pytest
from typer.testing import CliRunner

from vibepod.cli import app
from vibepod.core import providers

# The provider store enforces POSIX owner-only permissions and refuses other platforms.
pytestmark = pytest.mark.skipif(os.name == "nt", reason="provider store is POSIX-only")

runner = CliRunner()


@pytest.fixture
def existing(tmp_path, monkeypatch):
    monkeypatch.setenv("VP_PROVIDERS_DIR", str(tmp_path / "providers"))
    p = providers.Provider(
        "hosted",
        "openai-chat",
        "https://example.com/v1",
        auth="key",
        models=("a",),
        default_model="a",
    )
    providers.save_provider(p, key="private-key")
    return p


def test_update_preserves_existing_key(existing):
    providers.update_provider(replace(existing, models=("a", "b")))
    p = providers.load_provider("hosted")
    assert p.models == ("a", "b")
    assert providers.resolve_key(p) == "private-key"


def test_update_rotates_key(existing):
    providers.update_provider(existing, key="replacement-key")
    assert providers.resolve_key(providers.load_provider("hosted")) == "replacement-key"
    assert "replacement-key" not in (providers.provider_root() / "hosted/provider.toml").read_text()


def test_invalid_edit_preserves_previous_data(existing):
    with pytest.raises(ValueError):
        providers.update_provider(replace(existing, default_model="missing"), key="replacement")
    assert providers.load_provider("hosted") == existing
    assert providers.resolve_key(existing) == "private-key"


def test_metadata_write_failure_preserves_previous_key(existing, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("write failed")

    monkeypatch.setattr(providers, "_write_metadata", fail)
    with pytest.raises(OSError):
        providers.update_provider(existing, key="replacement")
    assert providers.load_provider("hosted") == existing
    assert providers.resolve_key(existing) == "private-key"


def test_switch_auth_removes_saved_key(existing):
    providers.update_provider(replace(existing, auth="env", key_env="MY_KEY"))
    files = list((providers.provider_root() / "hosted").glob("*credentials*.json"))
    assert files == []


def test_edit_keep_settings_and_secret(existing):
    # protocol, URL, auth, keep key, refresh, models, default
    result = runner.invoke(app, ["provider", "edit", "hosted"], input="\n\n\n\nn\n\n\n\n")
    assert result.exit_code == 0, result.output
    assert providers.load_provider("hosted") == existing
    assert providers.resolve_key(existing) == "private-key"
    assert "private-key" not in result.output


def test_edit_refresh_selects_new_catalog(existing, monkeypatch):
    monkeypatch.setattr("vibepod.commands.provider.discover_models", lambda *a, **k: ["a", "new"])
    result = runner.invoke(app, ["provider", "edit", "hosted"], input="\n\n\n\ny\ny\n\n\n\n")
    assert result.exit_code == 0, result.output
    p = providers.load_provider("hosted")
    assert p.models == ("a", "new")
    assert p.default_model == "a"
    assert providers.resolve_key(p) == "private-key"
    assert providers.load_models("hosted") == ["a", "new"]


def test_edit_failed_refresh_keeps_cache_and_selections(existing, monkeypatch):
    providers.save_models("hosted", ["a", "cached"])

    def fail(*args, **kwargs):
        raise ValueError("Endpoint unavailable")

    monkeypatch.setattr("vibepod.commands.provider.discover_models", fail)
    result = runner.invoke(app, ["provider", "edit", "hosted"], input="\n\n\n\ny\ny\n\n\n\n")
    assert result.exit_code == 0, result.output
    assert providers.load_provider("hosted") == existing
    assert providers.load_models("hosted") == ["a", "cached"]


def test_edit_replaces_key_without_echo(existing):
    result = runner.invoke(
        app,
        ["provider", "edit", "hosted"],
        input="\n\n\nn\nreplacement-key\ny\nn\n\n\n\n",
    )
    assert result.exit_code == 0, result.output
    assert providers.resolve_key(providers.load_provider("hosted")) == "replacement-key"
    assert "replacement-key" not in result.output
    assert "private-key" not in result.output


def test_credential_file_cannot_escape_store(existing):
    with pytest.raises(ValueError, match="credential file"):
        providers.update_provider(replace(existing, credential_file="../outside.json"))
    assert providers.resolve_key(existing) == "private-key"


def test_cancel_edit_preserves_existing(existing):
    result = runner.invoke(app, ["provider", "edit", "hosted"], input="\nhttps://other.example\n")
    assert result.exit_code != 0
    assert providers.load_provider("hosted") == existing
    assert providers.resolve_key(existing) == "private-key"


def test_edit_keeps_settings_and_drops_them_for_deselected_models(existing):
    from dataclasses import replace as dc_replace

    from vibepod.core.providers import ModelSettings

    providers.update_provider(
        dc_replace(
            existing,
            models=("a", "b"),
            model_settings={
                "a": ModelSettings(context_window=8192),
                "b": ModelSettings(reasoning=True, reasoning_default="low"),
            },
        ),
    )
    # protocol, URL, auth, keep key, refresh? n, models -> only "a", default, configure? n
    result = runner.invoke(app, ["provider", "edit", "hosted"], input="\n\n\n\nn\na\na\nn\n")
    assert result.exit_code == 0, result.output
    p = providers.load_provider("hosted")
    assert p.models == ("a",)
    assert p.model_settings == {"a": ModelSettings(context_window=8192)}


def test_edit_prefills_settings_and_updates_them(existing):
    from dataclasses import replace as dc_replace

    from vibepod.core.providers import ModelSettings

    providers.update_provider(
        dc_replace(existing, model_settings={"a": ModelSettings(context_window=8192)}),
    )
    # ... configure? y, models (empty = default "a"), context (Enter keeps 8192),
    # max output 2048, reasoning? y, levels (empty = all), default level medium
    result = runner.invoke(
        app,
        ["provider", "edit", "hosted"],
        input="\n\n\n\nn\n\n\ny\n\n\n2048\ny\n\nmedium\n",
    )
    assert result.exit_code == 0, result.output
    assert "8192" in result.output
    settings = providers.load_provider("hosted").model_settings["a"]
    assert settings == ModelSettings(
        context_window=8192,
        max_output_tokens=2048,
        reasoning=True,
        reasoning_default="medium",
    )
