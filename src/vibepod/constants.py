"""Constants and defaults for VibePod."""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_config_dir

APP_NAME = "vibepod"
VERSION = "0.20.1"

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
    "pi",
    "agy",
    "tau",
    "jcode",
    "freebuff",
    "qwen",
)

AGENT_SHORTCUTS: dict[str, str] = {
    "c": "claude",
    "g": "gemini",
    "o": "opencode",
    "d": "devstral",
    "a": "auggie",
    "p": "copilot",
    "x": "codex",
    "n": "agy",
    "t": "tau",
    "j": "jcode",
    "fb": "freebuff",
    "q": "qwen",
}

AGENT_ALIASES: dict[str, str] = {
    "vibe": "devstral",
    # The issue that added this agent calls it "qwen-cli"; the runtime
    # binary and image are `qwen` (Qwen Code, npm @qwen-code/qwen-code),
    # so `qwen` is the canonical id and `qwen-cli` is accepted as an alias.
    "qwen-cli": "qwen",
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
    "VP_IMAGE_PI",
    "VP_IMAGE_AGY",
    "VP_IMAGE_TAU",
    "VP_IMAGE_JCODE",
    "VP_IMAGE_FREEBUFF",
    "VP_IMAGE_QWEN",
    "VP_DATASETTE_IMAGE",
    "VP_PROXY_IMAGE",
    "VP_SKILLS_ENGINE_IMAGE",
)


def get_skills_engine_image() -> str:
    return os.environ.get(
        "VP_SKILLS_ENGINE_IMAGE",
        f"{os.environ.get('VP_IMAGE_NAMESPACE', 'vibepod')}/skills-engine:latest",
    )


SKILLS_ENGINE_IMAGE: str = get_skills_engine_image()

# Skill storage paths
USER_SKILLS_DIR = CONFIG_DIR / "skills"
PROJECT_SKILLS_DIR = Path(".vibepod") / "skills"
SKILLS_CACHE_DIR = CONFIG_DIR / "skills-cache"


def get_default_images() -> dict[str, str]:
    return {
        "claude": os.environ.get(
            "VP_IMAGE_CLAUDE",
            f"{os.environ.get('VP_IMAGE_NAMESPACE', 'vibepod')}/claude:latest",
        ),
        "gemini": os.environ.get(
            "VP_IMAGE_GEMINI",
            f"{os.environ.get('VP_IMAGE_NAMESPACE', 'vibepod')}/gemini:latest",
        ),
        "opencode": os.environ.get(
            "VP_IMAGE_OPENCODE",
            f"{os.environ.get('VP_IMAGE_NAMESPACE', 'vibepod')}/opencode:latest",
        ),
        "devstral": os.environ.get(
            "VP_IMAGE_DEVSTRAL",
            f"{os.environ.get('VP_IMAGE_NAMESPACE', 'vibepod')}/devstral:latest",
        ),
        "auggie": os.environ.get(
            "VP_IMAGE_AUGGIE",
            f"{os.environ.get('VP_IMAGE_NAMESPACE', 'vibepod')}/auggie:latest",
        ),
        "copilot": os.environ.get(
            "VP_IMAGE_COPILOT",
            f"{os.environ.get('VP_IMAGE_NAMESPACE', 'vibepod')}/copilot:latest",
        ),
        "codex": os.environ.get(
            "VP_IMAGE_CODEX",
            f"{os.environ.get('VP_IMAGE_NAMESPACE', 'vibepod')}/codex:latest",
        ),
        "pi": os.environ.get(
            "VP_IMAGE_PI",
            f"{os.environ.get('VP_IMAGE_NAMESPACE', 'vibepod')}/pi:latest",
        ),
        "agy": os.environ.get(
            "VP_IMAGE_AGY",
            f"{os.environ.get('VP_IMAGE_NAMESPACE', 'vibepod')}/agy:latest",
        ),
        "tau": os.environ.get(
            "VP_IMAGE_TAU",
            f"{os.environ.get('VP_IMAGE_NAMESPACE', 'vibepod')}/tau:latest",
        ),
        "jcode": os.environ.get(
            "VP_IMAGE_JCODE",
            f"{os.environ.get('VP_IMAGE_NAMESPACE', 'vibepod')}/jcode:latest",
        ),
        "freebuff": os.environ.get(
            "VP_IMAGE_FREEBUFF",
            f"{os.environ.get('VP_IMAGE_NAMESPACE', 'vibepod')}/freebuff:latest",
        ),
        "qwen": os.environ.get(
            "VP_IMAGE_QWEN",
            f"{os.environ.get('VP_IMAGE_NAMESPACE', 'vibepod')}/qwen:latest",
        ),
        "datasette": os.environ.get(
            "VP_DATASETTE_IMAGE",
            f"{os.environ.get('VP_IMAGE_NAMESPACE', 'vibepod')}/datasette:latest",
        ),
        "proxy": os.environ.get(
            "VP_PROXY_IMAGE",
            f"{os.environ.get('VP_IMAGE_NAMESPACE', 'vibepod')}/proxy:latest",
        ),
        "skills-engine": get_skills_engine_image(),
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
