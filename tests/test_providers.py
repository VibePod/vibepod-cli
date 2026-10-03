"""Provider registry security and round-trip behavior."""

import json
import os
import stat

import pytest

from vibepod.core import providers

# The provider store enforces POSIX owner-only permissions and refuses other platforms.
pytestmark = pytest.mark.skipif(os.name == "nt", reason="provider store is POSIX-only")


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "providers"
    monkeypatch.setenv("VP_PROVIDERS_DIR", str(root))
    return root


def definition(name="local", **kwargs):
    return providers.Provider(
        name=name,
        protocol="openai-chat",
        base_url="http://localhost:11434/v1",
        **kwargs,
    )


def test_round_trip_keeps_credentials_separate(store):
    p = definition(auth="key", models=("model-a",), default_model="model-a")
    providers.save_provider(p, key="secret-value")
    assert providers.load_provider("local") == p
    assert providers.resolve_key(p) == "secret-value"
    assert "secret-value" not in (store / "local" / "provider.toml").read_text()
    assert providers.list_providers() == ["local"]
    assert stat.S_IMODE(store.stat().st_mode) == 0o700
    for path in (store / "local").iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("name", ["../escape", "/absolute", "UPPER", "", "a/b", "."])
def test_invalid_names_rejected(store, name):
    with pytest.raises(ValueError):
        providers.save_provider(definition(name))
    assert not store.exists()


def test_duplicate_does_not_replace_key(store):
    providers.save_provider(definition(auth="key"), key="first")
    with pytest.raises(ValueError, match="exists"):
        providers.save_provider(definition(auth="key"), key="second")
    assert providers.resolve_key(providers.load_provider("local")) == "first"


def test_environment_reference_resolved_at_use(store, monkeypatch):
    p = definition(auth="env", key_env="LOCAL_KEY")
    providers.save_provider(p)
    with pytest.raises(ValueError, match="LOCAL_KEY"):
        providers.resolve_key(p)
    monkeypatch.setenv("LOCAL_KEY", "later")
    assert providers.resolve_key(p) == "later"
    assert not (store / "local" / "credentials.json").exists()


def test_symlink_provider_refused(store, tmp_path):
    store.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    (store / "local").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="[Ss]ymlink"):
        providers.load_provider("local")
    with pytest.raises(ValueError):
        providers.remove_provider("local")
    assert outside.exists()


def test_symlinked_ancestor_above_root_allowed(tmp_path, monkeypatch):
    real = tmp_path / "real-home"
    real.mkdir()
    link = tmp_path / "home-link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("VP_PROVIDERS_DIR", str(link / "providers"))
    providers.save_provider(definition())
    assert providers.list_providers() == ["local"]
    assert (real / "providers" / "local" / "provider.toml").exists()


def test_permission_error_names_path_and_fix(store):
    providers.save_provider(definition())
    store.chmod(0o755)
    with pytest.raises(ValueError, match=r"chmod 700 .*providers") as exc:
        providers.list_providers()
    assert str(store) in str(exc.value)


def test_anthropic_url_must_not_end_with_v1(store):
    with pytest.raises(ValueError, match="/v1"):
        providers.save_provider(providers.Provider("bad", "anthropic", "https://example.com/v1/"))
    assert not (store / "bad").exists()


def test_legacy_runtime_url_metadata_is_ignored(store):
    providers.save_provider(definition())
    path = store / "local" / "provider.toml"
    path.write_text(path.read_text() + 'runtime_url = "http://host.docker.internal:11434/v1"\n')
    assert providers.load_provider("local").base_url == "http://localhost:11434/v1"


def test_symlink_root_refused(store, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    store.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="[Ss]ymlink"):
        providers.save_provider(definition())
    assert list(target.iterdir()) == []


def test_world_readable_credentials_refused(store):
    p = definition(auth="key")
    providers.save_provider(p, key="private")
    (store / "local" / "credentials.json").chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        providers.resolve_key(p)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:key@example.com/v1",
        "http://",
        "https://example.com/?key=secret",
        "https://example.com/#fragment",
        "http://host/\x7f",
    ],
)
def test_unsafe_urls_rejected(store, url):
    with pytest.raises(ValueError):
        providers.save_provider(providers.Provider("bad", "openai-chat", url))


def test_unknown_protocol_and_invalid_default_rejected(store):
    with pytest.raises(ValueError):
        providers.save_provider(providers.Provider("bad", "unknown", "https://example.com"))
    with pytest.raises(ValueError):
        providers.save_provider(definition(models=("a",), default_model="b"))


def test_cache_and_removal(store):
    providers.save_provider(definition())
    providers.save_models("local", ["b", "a", "a"])
    assert providers.load_models("local") == ["a", "b"]
    data = json.loads((store / "local" / "models.json").read_text())
    assert data["refreshed_at"]
    providers.remove_provider("local")
    assert providers.list_providers() == []


def test_missing_provider_has_actionable_error(store):
    with pytest.raises(ValueError, match="does not exist"):
        providers.load_provider("missing")


def test_model_settings_round_trip_and_rendering(store):
    from vibepod.core.providers import ModelSettings

    settings = {
        "model-a": ModelSettings(
            context_window=32768,
            max_output_tokens=8192,
            reasoning_levels=("low", "medium", "high"),
            reasoning_default="medium",
        ),
        "model-b": ModelSettings(reasoning=False),
    }
    p = definition(models=("model-a", "model-b"), default_model="model-a", model_settings=settings)
    providers.save_provider(p)
    loaded = providers.load_provider("local")
    assert loaded == p
    assert loaded.model_settings["model-a"].effective_reasoning is True
    assert loaded.model_settings["model-b"].effective_reasoning is False
    text = (store / "local" / "provider.toml").read_text()
    assert '[model_settings."model-a"]' in text
    assert 'reasoning_levels = ["low", "medium", "high"]' in text
    assert "context_window = 32768" in text


@pytest.mark.parametrize(
    "settings, message",
    [
        ({"missing": providers.ModelSettings(context_window=1)}, "unselected"),
        ({"model-a": providers.ModelSettings(context_window=0)}, "positive integer"),
        ({"model-a": providers.ModelSettings(reasoning_levels=("ultra",))}, "Reasoning levels"),
        ({"model-a": providers.ModelSettings(reasoning_levels=("low", "low"))}, "distinct"),
        (
            {
                "model-a": providers.ModelSettings(
                    reasoning_levels=("low",),
                    reasoning_default="high",
                ),
            },
            "must be one of",
        ),
        (
            {"model-a": providers.ModelSettings(reasoning=False, reasoning_levels=("low",))},
            "non-reasoning",
        ),
    ],
)
def test_invalid_model_settings_rejected(store, settings, message):
    with pytest.raises(ValueError, match=message):
        providers.save_provider(definition(models=("model-a",), model_settings=settings))
