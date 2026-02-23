"""Constants and defaults for VibePod."""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_config_dir

APP_NAME = "vibepod"
VERSION = "0.2.0"

CONFIG_DIR = Path(user_config_dir(APP_NAME))
GLOBAL_CONFIG_FILE = CONFIG_DIR / "config.yaml"
PROJECT_CONFIG_FILE = Path(".vibepod") / "config.yaml"
LOGS_DB_FILE = CONFIG_DIR / "logs.db"

DOCKER_NETWORK = "vibepod-network"
CONTAINER_LABEL_MANAGED = "vibepod.managed"

SUPPORTED_AGENTS = ("claude", "gemini", "opencode", "devstral", "auggie", "copilot", "codex")

DEFAULT_IMAGES: dict[str, str] = {
    "claude": os.environ.get(
        "VP_IMAGE_CLAUDE", f"{os.environ.get('VP_IMAGE_NAMESPACE', 'nezhar')}/claude-container:latest"
    ),
    "gemini": os.environ.get(
        "VP_IMAGE_GEMINI", f"{os.environ.get('VP_IMAGE_NAMESPACE', 'nezhar')}/gemini-container:latest"
    ),
    "opencode": os.environ.get(
        "VP_IMAGE_OPENCODE", f"{os.environ.get('VP_IMAGE_NAMESPACE', 'nezhar')}/opencode-cli:latest"
    ),
    "devstral": os.environ.get(
        "VP_IMAGE_DEVSTRAL", f"{os.environ.get('VP_IMAGE_NAMESPACE', 'nezhar')}/devstral-cli:latest"
    ),
    "auggie": os.environ.get(
        "VP_IMAGE_AUGGIE", f"{os.environ.get('VP_IMAGE_NAMESPACE', 'nezhar')}/auggie-cli:latest"
    ),
    "copilot": os.environ.get(
        "VP_IMAGE_COPILOT",
        f"{os.environ.get('VP_IMAGE_NAMESPACE', 'nezhar')}/copilot-cli:latest",
    ),
    "codex": os.environ.get(
        "VP_IMAGE_CODEX", f"{os.environ.get('VP_IMAGE_NAMESPACE', 'nezhar')}/codex-cli:latest"
    ),
    "datasette": os.environ.get(
        "VP_DATASETTE_IMAGE", f"{os.environ.get('VP_IMAGE_NAMESPACE', 'vibepod')}/datasette:latest"
    ),
    "proxy": os.environ.get(
        "VP_PROXY_IMAGE", f"{os.environ.get('VP_IMAGE_NAMESPACE', 'vibepod')}/proxy:latest"
    ),
}

DEFAULT_ALIASES: dict[str, str] = {
    "c": "run claude",
    "g": "run gemini",
    "o": "run opencode",
    "d": "run devstral",
    "a": "run auggie",
    "p": "run copilot",
    "x": "run codex",
    "ui": "logs start",
}

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_INVALID_ARGS = 2
EXIT_DOCKER_NOT_RUNNING = 3
EXIT_IMAGE_NOT_FOUND = 4
EXIT_CONTAINER_ERROR = 7
EXIT_CONFIG_ERROR = 8
