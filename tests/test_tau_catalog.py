"""Tau catalog rendering in the container-side Python bootstrap."""

import importlib.util
import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

import vibepod

BOOTSTRAP = Path(vibepod.__file__).parent / "resources" / "provider-bootstrap.py"
ALL_LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh"]


def _load_bootstrap():
    spec = importlib.util.spec_from_file_location("vp_provider_bootstrap", BOOTSTRAP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = _load_bootstrap()


def _provider(settings, models=("big", "plain"), default="big", protocol="openai-chat"):
    return {
        "protocol": protocol,
        "baseUrl": "https://example.com/v1",
        "apiKeyEnv": "VP_KEY",
        "models": list(models),
        "defaultModel": default,
        "settings": settings,
    }


def _render(settings, **kwargs):
    text = bootstrap.render_tau_catalog("", {"tuned": _provider(settings, **kwargs)})
    [entry] = tomllib.loads(text)["providers"]
    return text, entry


def _assert_scalars_before_subtables(text):
    lines = text.splitlines()
    first_table = next(i for i, line in enumerate(lines) if line.startswith("[providers."))
    assert not any(line.startswith("thinking_") for line in lines[first_table:]), (
        "provider scalars must precede model_metadata sub-tables"
    )
    keys = [line.split(" = ")[0] for line in lines if line.startswith("thinking_")]
    assert keys[0] == "thinking_levels", keys


def test_reasoning_model_without_levels_offers_all_levels():
    text, entry = _render({"big": {"reasoning": True}, "plain": {"reasoning": False}})
    assert entry["thinking_levels"] == ALL_LEVELS
    assert entry["thinking_parameter"] == "reasoning_effort"
    assert "thinking_default" not in entry
    assert entry["model_metadata"]["big"] == {"reasoning": True}
    assert entry["model_metadata"]["plain"] == {"reasoning": False}
    _assert_scalars_before_subtables(text)


def test_reasoning_model_with_levels_and_default():
    text, entry = _render(
        {
            "big": {
                "reasoning": True,
                "reasoningLevels": ["xhigh", "low", "high"],
                "reasoningDefault": "high",
            },
            "plain": {"reasoning": False},
        },
    )
    assert entry["thinking_levels"] == ["low", "high", "xhigh"]
    assert entry["thinking_default"] == "high"
    assert "unsupported_thinking_levels" not in entry["model_metadata"]["big"]
    _assert_scalars_before_subtables(text)


def test_levels_union_in_tau_order_and_per_model_unsupported_levels():
    text, entry = _render(
        {
            "a": {"reasoning": True, "reasoningLevels": ["high", "medium"]},
            "b": {"reasoning": True, "reasoningLevels": ["off", "low", "max"]},
            "c": {"contextWindow": 4096},
            "d": {"reasoning": False},
        },
        models=("a", "b", "c", "d"),
        default="a",
        protocol="anthropic",
    )
    assert entry["thinking_levels"] == ["off", "low", "medium", "high"]
    assert entry["thinking_parameter"] == "anthropic.thinking"
    metadata = entry["model_metadata"]
    assert metadata["a"]["unsupported_thinking_levels"] == ["off", "low"]
    assert metadata["b"]["unsupported_thinking_levels"] == ["medium", "high"]
    assert metadata["c"] == {"context_window": 4096}
    assert metadata["d"] == {"reasoning": False}
    _assert_scalars_before_subtables(text)


def test_default_outside_levels_is_omitted_with_comment():
    # The default model is not a reasoning model; another model offers the levels.
    text, entry = _render(
        {
            "big": {"reasoning": True, "reasoningLevels": ["low"], "reasoningDefault": "low"},
            "plain": {"reasoning": True, "reasoningLevels": ["high"], "reasoningDefault": "high"},
        },
        default="big",
    )
    assert entry["thinking_default"] == "low"
    text, entry = _render(
        {
            "big": {"reasoningDefault": "max"},
            "plain": {"reasoning": True, "reasoningLevels": ["high"]},
        },
    )
    assert entry["thinking_levels"] == ["high"]
    assert "thinking_default" not in entry
    assert '# thinking_default "max" omitted' in text


def test_no_reasoning_models_writes_no_thinking_keys():
    _, entry = _render({"big": {"reasoning": False}, "plain": {"contextWindow": 1024}})
    assert not any(key.startswith("thinking_") for key in entry)


@pytest.mark.parametrize(
    "settings",
    [
        {"big": {"reasoning": True}},
        {"big": {"reasoning": True, "reasoningLevels": ["low", "high"], "reasoningDefault": "low"}},
        {
            "big": {"reasoning": True, "reasoningLevels": ["medium"], "reasoningDefault": "medium"},
            "plain": {"reasoning": True},
        },
    ],
)
def test_tau_loader_accepts_rendered_catalog(tmp_path, settings):
    pytest.importorskip("tau_coding", reason="tau-ai is not installed")
    from tau_coding.catalog_loader import effective_catalog
    from tau_coding.paths import TauPaths
    from tau_coding.provider_config import provider_config_from_entry

    home = tmp_path / ".tau"
    home.mkdir()
    (home / "catalog.toml").write_text(
        bootstrap.render_tau_catalog("", {"tuned": _provider(settings)}),
    )
    [entry] = [e for e in effective_catalog(TauPaths(home=home)) if e.name == "tuned"]
    config = provider_config_from_entry(entry)
    assert config.thinking_levels
