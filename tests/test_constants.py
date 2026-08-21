"""Constants and default-image mapping tests."""

from __future__ import annotations

from vibepod.constants import get_default_images


def test_default_images_match_documented_registry_defaults(monkeypatch) -> None:
    for key in (
        "VP_IMAGE_NAMESPACE",
        "VP_IMAGE_CLAUDE",
        "VP_IMAGE_GEMINI",
        "VP_IMAGE_OPENCODE",
        "VP_IMAGE_DEVSTRAL",
        "VP_IMAGE_AUGGIE",
        "VP_IMAGE_COPILOT",
        "VP_IMAGE_CODEX",
        "VP_IMAGE_PI",
        "VP_IMAGE_AGY",
        "VP_IMAGE_TAU",
        "VP_IMAGE_JCODE",
        "VP_IMAGE_FREEBUFF",
        "VP_IMAGE_QWEN",
        "VP_IMAGE_DSH",
        "VP_DATASETTE_IMAGE",
        "VP_PROXY_IMAGE",
    ):
        monkeypatch.delenv(key, raising=False)

    images = get_default_images()

    assert images["claude"] == "vibepod/claude:latest"
    assert images["gemini"] == "vibepod/gemini:latest"
    assert images["opencode"] == "vibepod/opencode:latest"
    assert images["devstral"] == "vibepod/devstral:latest"
    assert images["auggie"] == "vibepod/auggie:latest"
    assert images["copilot"] == "vibepod/copilot:latest"
    assert images["codex"] == "vibepod/codex:latest"
    assert images["pi"] == "vibepod/pi:latest"
    assert images["agy"] == "vibepod/agy:latest"
    assert images["tau"] == "vibepod/tau:latest"
    assert images["jcode"] == "vibepod/jcode:latest"
    assert images["freebuff"] == "vibepod/freebuff:latest"
    assert images["qwen"] == "vibepod/qwen:latest"
    assert images["dsh"] == "vibepod/dsh:latest"
    assert images["datasette"] == "vibepod/datasette:latest"
    assert images["proxy"] == "vibepod/proxy:latest"


def test_pi_image_override(monkeypatch) -> None:
    monkeypatch.setenv("VP_IMAGE_PI", "example/pi:dev")

    images = get_default_images()

    assert images["pi"] == "example/pi:dev"


def test_agy_image_override(monkeypatch) -> None:
    monkeypatch.setenv("VP_IMAGE_AGY", "example/agy:dev")

    images = get_default_images()

    assert images["agy"] == "example/agy:dev"


def test_tau_image_override(monkeypatch) -> None:
    monkeypatch.setenv("VP_IMAGE_TAU", "example/tau:dev")

    images = get_default_images()

    assert images["tau"] == "example/tau:dev"


def test_jcode_image_override(monkeypatch) -> None:
    monkeypatch.setenv("VP_IMAGE_JCODE", "example/jcode:dev")

    images = get_default_images()

    assert images["jcode"] == "example/jcode:dev"


def test_freebuff_image_override(monkeypatch) -> None:
    monkeypatch.setenv("VP_IMAGE_FREEBUFF", "example/freebuff:dev")

    images = get_default_images()

    assert images["freebuff"] == "example/freebuff:dev"


def test_qwen_image_override(monkeypatch) -> None:
    monkeypatch.setenv("VP_IMAGE_QWEN", "example/qwen:dev")

    images = get_default_images()

    assert images["qwen"] == "example/qwen:dev"


def test_dsh_image_override(monkeypatch) -> None:
    monkeypatch.setenv("VP_IMAGE_DSH", "example/dsh:dev")

    images = get_default_images()

    assert images["dsh"] == "example/dsh:dev"
