"""Constants and defaults for VibePod."""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_config_dir

APP_NAME = "vibepod"
VERSION = "0.5.0"

CONFIG_DIR = Path(user_config_dir(APP_NAME))
GLOBAL_CONFIG_FILE = CONFIG_DIR / "config.yaml"
PROJECT_CONFIG_FILE = Path(".vibepod") / "config.yaml"
LOGS_DB_FILE = CONFIG_DIR / "logs.db"

DOCKER_NETWORK = "vibepod-network"
CONTAINER_LABEL_MANAGED = "vibepod.managed"

SUPPORTED_AGENTS = (
    "claude",
    "gemini",
    "opencode",
    "devstral",
    "auggie",
    "copilot",
    "codex",
)

AGENT_SHORTCUTS: dict[str, str] = {
    "c": "claude",
    "g": "gemini",
    "o": "opencode",
    "d": "devstral",
    "a": "auggie",
    "p": "copilot",
    "x": "codex",
}

IMAGE_OVERRIDE_ENV_KEYS: tuple[str, ...] = (
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
)


def get_default_images() -> dict[str, str]:
    return {
        "claude": os.environ.get(
            "VP_IMAGE_CLAUDE",
            f"{os.environ.get('VP_IMAGE_NAMESPACE', 'vibepod')}/claude:latest",
        ),
        "gemini": os.environ.get(
            "VP_IMAGE_GEMINI",
            f"{os.environ.get('VP_IMAGE_NAMESPACE', 'nezhar')}/gemini-container:latest",
        ),
        "opencode": os.environ.get(
            "VP_IMAGE_OPENCODE",
            f"{os.environ.get('VP_IMAGE_NAMESPACE', 'nezhar')}/opencode-cli:latest",
        ),
        "devstral": os.environ.get(
            "VP_IMAGE_DEVSTRAL",
            f"{os.environ.get('VP_IMAGE_NAMESPACE', 'nezhar')}/devstral-cli:latest",
        ),
        "auggie": os.environ.get(
            "VP_IMAGE_AUGGIE",
            f"{os.environ.get('VP_IMAGE_NAMESPACE', 'nezhar')}/auggie-cli:latest",
        ),
        "copilot": os.environ.get(
            "VP_IMAGE_COPILOT",
            f"{os.environ.get('VP_IMAGE_NAMESPACE', 'nezhar')}/copilot-cli:latest",
        ),
        "codex": os.environ.get(
            "VP_IMAGE_CODEX", f"{os.environ.get('VP_IMAGE_NAMESPACE', 'vibepod')}/codex:latest"
        ),
        "datasette": os.environ.get(
            "VP_DATASETTE_IMAGE",
            f"{os.environ.get('VP_IMAGE_NAMESPACE', 'vibepod')}/datasette:latest",
        ),
        "proxy": os.environ.get(
            "VP_PROXY_IMAGE", f"{os.environ.get('VP_IMAGE_NAMESPACE', 'vibepod')}/proxy:latest"
        ),
    }


DEFAULT_IMAGES: dict[str, str] = get_default_images()

DEFAULT_ALIASES: dict[str, str] = {
    **{shortcut: f"run {agent}" for shortcut, agent in AGENT_SHORTCUTS.items()},
    "ui": "logs start",
}

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_INVALID_ARGS = 2
EXIT_DOCKER_NOT_RUNNING = 3
EXIT_IMAGE_NOT_FOUND = 4
EXIT_CONTAINER_ERROR = 7
EXIT_CONFIG_ERROR = 8
