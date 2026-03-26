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
    assert images["datasette"] == "vibepod/datasette:latest"
    assert images["proxy"] == "vibepod/proxy:latest"
