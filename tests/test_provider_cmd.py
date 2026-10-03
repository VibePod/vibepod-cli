"""Provider management command tests."""

import os

import pytest
from typer.testing import CliRunner

from vibepod.cli import app
from vibepod.core.providers import Provider, load_provider, resolve_key, save_provider

# The provider store enforces POSIX owner-only permissions and refuses other platforms.
pytestmark = pytest.mark.skipif(os.name == "nt", reason="provider store is POSIX-only")

runner = CliRunner()


def test_wizard_manual_local_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("VP_PROVIDERS_DIR", str(tmp_path / "providers"))
    result = runner.invoke(
        app,
        ["provider", "add"],
        input="local\nopenai-chat\nhttp://localhost:11434/v1\nnone\nn\nqwen3:8b\nqwen3:8b\n\n",
    )
    assert result.exit_code == 0, result.output
    assert "localhost is the container itself" in result.output
    p = load_provider("local")
    assert p.models == ("qwen3:8b",)
    assert resolve_key(p) == ""
    assert "local" in runner.invoke(app, ["provider", "list"]).output
    assert "qwen3:8b" in runner.invoke(app, ["provider", "models", "local"]).output


@pytest.mark.parametrize("answer", ["", "y"])
def test_wizard_selects_all_discovered_models(tmp_path, monkeypatch, answer):
    monkeypatch.setenv("VP_PROVIDERS_DIR", str(tmp_path / "providers"))
    monkeypatch.setattr(
        "vibepod.commands.provider.discover_models",
        lambda *args, **kwargs: ["a", "b"],
    )
    result = runner.invoke(
        app,
        ["provider", "add"],
        input=f"local\nopenai-chat\nhttps://example.com/v1\nnone\ny\ny\n{answer}\na\n\n",
    )
    assert result.exit_code == 0, result.output
    assert "Use all discovered models?" in result.output
    assert "Model IDs (comma-separated)" not in result.output
    assert load_provider("local").models == ("a", "b")
    assert load_provider("local").default_model == "a"


def test_wizard_can_decline_all_models(tmp_path, monkeypatch):
    monkeypatch.setenv("VP_PROVIDERS_DIR", str(tmp_path / "providers"))
    monkeypatch.setattr(
        "vibepod.commands.provider.discover_models",
        lambda *args, **kwargs: ["a", "b"],
    )
    result = runner.invoke(
        app,
        ["provider", "add"],
        input="local\nopenai-chat\nhttps://example.com/v1\nnone\ny\ny\nn\nb\nb\n\n",
    )
    assert result.exit_code == 0, result.output
    assert load_provider("local").models == ("b",)


@pytest.mark.parametrize("fails", [False, True])
def test_wizard_manual_models_when_discovery_empty_or_failed(tmp_path, monkeypatch, fails):
    monkeypatch.setenv("VP_PROVIDERS_DIR", str(tmp_path / "providers"))

    def discover(*args, **kwargs):
        if fails:
            raise ValueError("Model listing unsupported")
        return []

    monkeypatch.setattr("vibepod.commands.provider.discover_models", discover)
    result = runner.invoke(
        app,
        ["provider", "add"],
        input="local\nopenai-chat\nhttps://example.com/v1\nnone\ny\ny\nmanual\nmanual\n\n",
    )
    assert result.exit_code == 0, result.output
    assert "Use all discovered models?" not in result.output
    assert load_provider("local").models == ("manual",)


@pytest.mark.parametrize(
    "answers, message",
    [
        ("local\nbogus\n", "Protocol"),
        ("local\nopenai-chat\nhttps://example.com/v1\nkeys\n", "Authentication"),
    ],
)
def test_wizard_validates_choices_before_asking_for_a_key(tmp_path, monkeypatch, answers, message):
    monkeypatch.setenv("VP_PROVIDERS_DIR", str(tmp_path / "providers"))
    result = runner.invoke(app, ["provider", "add"], input=answers)
    assert result.exit_code == 1
    assert message in result.output
    assert "API key" not in result.output


def test_list_reports_broken_provider_and_continues(tmp_path, monkeypatch):
    root = tmp_path / "providers"
    monkeypatch.setenv("VP_PROVIDERS_DIR", str(root))
    save_provider(Provider("broken", "openai-chat", "https://example.com/v1"))
    save_provider(Provider("good", "openai-chat", "https://example.com/v1"))
    (root / "broken" / "provider.toml").write_text("version = 2\n")
    result = runner.invoke(app, ["provider", "list"])
    assert result.exit_code == 0, result.output
    assert "good  openai-chat" in result.output
    assert "broken" in result.output and "Invalid metadata" in result.output


def test_wizard_stores_masked_key(tmp_path, monkeypatch):
    monkeypatch.setenv("VP_PROVIDERS_DIR", str(tmp_path / "providers"))
    result = runner.invoke(
        app,
        ["provider", "add"],
        input="hosted\nanthropic\nhttps://example.com\nkey\nprivate-key\ny\nn\nmodel-a\nmodel-a\n\n",
    )
    assert result.exit_code == 0, result.output
    assert "private-key" not in result.output
    assert resolve_key(load_provider("hosted")) == "private-key"
    result = runner.invoke(app, ["provider", "remove", "hosted", "--yes"])
    assert result.exit_code == 0


def test_invalid_provider_fails_without_traceback(tmp_path, monkeypatch):
    monkeypatch.setenv("VP_PROVIDERS_DIR", str(tmp_path / "providers"))
    result = runner.invoke(app, ["provider", "models", "missing"])
    assert result.exit_code == 1
    assert "does not exist" in result.output


def test_failed_refresh_preserves_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("VP_PROVIDERS_DIR", str(tmp_path / "providers"))
    save_provider(Provider("local", "openai-chat", "http://localhost:11434/v1", models=("a",)))

    def fail(*args, **kwargs):
        raise ValueError("Cannot reach model endpoint")

    monkeypatch.setattr("vibepod.commands.provider.discover_models", fail)
    result = runner.invoke(app, ["provider", "models", "local", "--refresh"], input="y\n")
    assert result.exit_code == 1
    assert load_provider("local").models == ("a",)


def test_wizard_configures_model_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("VP_PROVIDERS_DIR", str(tmp_path / "providers"))
    # name, protocol, url, auth, discover? n, models, default, configure? y,
    # models to configure (empty = default), context, max output, reasoning? y,
    # levels, default level
    result = runner.invoke(
        app,
        ["provider", "add"],
        input=(
            "local\nopenai-chat\nhttp://ollama:11434/v1\nnone\nn\nbig, small\nbig\n"
            "y\n\n32768\n\ny\nlow, high\nhigh\n"
        ),
    )
    assert result.exit_code == 0, result.output
    p = load_provider("local")
    assert p.model_settings["big"].context_window == 32768
    assert p.model_settings["big"].max_output_tokens is None
    assert p.model_settings["big"].reasoning is True
    assert p.model_settings["big"].reasoning_levels == ("low", "high")
    assert p.model_settings["big"].reasoning_default == "high"
    assert "small" not in p.model_settings


def test_wizard_rejects_bad_settings_without_saving(tmp_path, monkeypatch):
    monkeypatch.setenv("VP_PROVIDERS_DIR", str(tmp_path / "providers"))
    result = runner.invoke(
        app,
        ["provider", "add"],
        input="local\nopenai-chat\nhttp://ollama:11434/v1\nnone\nn\nbig\nbig\ny\nbig\nlots\n",
    )
    assert result.exit_code == 1
    assert "positive integer" in result.output
    assert not (tmp_path / "providers" / "local").exists()


def test_refresh_reselects_models_and_keeps_settings_of_kept_ones(tmp_path, monkeypatch):

    from vibepod.core.providers import ModelSettings

    monkeypatch.setenv("VP_PROVIDERS_DIR", str(tmp_path / "providers"))
    save_provider(
        Provider(
            "local",
            "openai-chat",
            "http://ollama:11434/v1",
            models=("old", "kept"),
            default_model="old",
            model_settings={
                "old": ModelSettings(context_window=1024),
                "kept": ModelSettings(context_window=4096),
            },
        ),
    )
    monkeypatch.setattr(
        "vibepod.commands.provider.discover_models",
        lambda *a, **k: ["kept", "new"],
    )
    # contact endpoint? y, use all discovered? y, default -> new
    result = runner.invoke(app, ["provider", "refresh", "local"], input="y\ny\nnew\n")
    assert result.exit_code == 0, result.output
    p = load_provider("local")
    assert p.models == ("kept", "new")
    assert p.default_model == "new"
    assert p.model_settings == {"kept": ModelSettings(context_window=4096)}
    assert p.base_url == "http://ollama:11434/v1"
    assert "Model IDs (comma-separated)" not in result.output
    assert runner.invoke(app, ["provider", "models", "local"]).output.count("\n") >= 2


def test_refresh_failure_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("VP_PROVIDERS_DIR", str(tmp_path / "providers"))
    save_provider(
        Provider(
            "local",
            "openai-chat",
            "http://ollama:11434/v1",
            models=("a",),
            default_model="a",
        ),
    )
    before = load_provider("local")

    def fail(*args, **kwargs):
        raise ValueError("Cannot reach model endpoint")

    monkeypatch.setattr("vibepod.commands.provider.discover_models", fail)
    result = runner.invoke(app, ["provider", "refresh", "local"], input="y\n")
    assert result.exit_code == 1
    assert "Cannot reach" in result.output
    assert load_provider("local") == before


def test_add_leaves_nothing_behind_when_the_cache_write_fails(tmp_path, monkeypatch):
    from vibepod.core import providers

    root = tmp_path / "providers"
    monkeypatch.setenv("VP_PROVIDERS_DIR", str(root))
    monkeypatch.setattr(
        "vibepod.commands.provider.discover_models",
        lambda *a, **k: ["a", "b"],
    )
    real_write = providers._write_json

    def flaky(path, value):
        if path.name == "models.json":
            raise OSError("disk full")
        real_write(path, value)

    monkeypatch.setattr(providers, "_write_json", flaky)
    result = runner.invoke(
        app,
        ["provider", "add"],
        input="local\nopenai-chat\nhttps://example.com/v1\nnone\ny\ny\ny\na\n\n",
    )
    assert result.exit_code == 1
    assert not (root / "local").exists(), "half-created provider must not remain"
    assert not list(root.glob(".create-*")), "staging directory must be cleaned up"


def test_refresh_keeps_selection_when_metadata_write_fails(tmp_path, monkeypatch):
    from vibepod.core import providers

    monkeypatch.setenv("VP_PROVIDERS_DIR", str(tmp_path / "providers"))
    save_provider(
        Provider(
            "local",
            "openai-chat",
            "http://ollama:11434/v1",
            models=("a",),
            default_model="a",
        ),
    )
    monkeypatch.setattr(
        "vibepod.commands.provider.discover_models",
        lambda *a, **k: ["a", "new"],
    )

    def fail(*args, **kwargs):
        raise OSError("metadata write failed")

    monkeypatch.setattr(providers, "_write_metadata", fail)
    result = runner.invoke(app, ["provider", "refresh", "local"], input="y\ny\nnew\n")
    assert result.exit_code == 1
    p = load_provider("local")
    assert p.models == ("a",) and p.default_model == "a"
    # The cache reflects the discovery that did happen.
    assert providers.load_models("local") == ["a", "new"]


def test_wizard_declining_discovery_falls_back_to_manual_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("VP_PROVIDERS_DIR", str(tmp_path / "providers"))

    def unexpected(*args, **kwargs):
        raise AssertionError("endpoint must not be contacted after declining")

    monkeypatch.setattr("vibepod.commands.provider.discover_models", unexpected)
    # name, protocol, url, auth key + key + store? y, discover? y, send over HTTP? n,
    # manual models, default, configure settings? n
    result = runner.invoke(
        app,
        ["provider", "add"],
        input="local\nopenai-chat\nhttp://ollama.local:11434/v1\nkey\nsecret\ny\ny\nn\nmanual\nmanual\n\n",
    )
    assert result.exit_code == 0, result.output
    assert "Discovery skipped" in result.output
    assert load_provider("local").models == ("manual",)
    assert "secret" not in result.output


def test_edit_declining_discovery_keeps_previous_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("VP_PROVIDERS_DIR", str(tmp_path / "providers"))
    save_provider(
        Provider(
            "local",
            "openai-chat",
            "https://example.com/v1",
            models=("a",),
            default_model="a",
        ),
    )
    # protocol, url, auth, refresh? y, contact endpoint? n, models (keep), default, settings? n
    result = runner.invoke(app, ["provider", "edit", "local"], input="\n\n\ny\nn\n\n\n\n")
    assert result.exit_code == 0, result.output
    assert load_provider("local").models == ("a",)


def test_refresh_declining_discovery_aborts(tmp_path, monkeypatch):
    monkeypatch.setenv("VP_PROVIDERS_DIR", str(tmp_path / "providers"))
    save_provider(Provider("local", "openai-chat", "https://example.com/v1", models=("a",)))
    result = runner.invoke(app, ["provider", "refresh", "local"], input="n\n")
    assert result.exit_code != 0
    assert load_provider("local").models == ("a",)
