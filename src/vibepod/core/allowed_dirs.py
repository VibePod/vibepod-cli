"""Allowed directories management for vp run."""

from __future__ import annotations

import json
import os
from pathlib import Path

from vibepod.core.config import get_config_root


def get_allowed_dirs_path() -> Path:
    """Return path to the allowed-directories JSON file."""
    return get_config_root() / "allowed_dirs.json"


def load_allowed_dirs() -> list[str]:
    """Load and return the list of allowed directory paths."""
    path = get_allowed_dirs_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(d) for d in data if isinstance(d, str)]
    except (json.JSONDecodeError, OSError):
        pass
    return []


def save_allowed_dirs(dirs: list[str]) -> None:
    """Persist the list of allowed directory paths."""
    path = get_allowed_dirs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(set(dirs)), indent=2), encoding="utf-8")
    os.replace(tmp, path)


def is_protected_dir(path: Path) -> bool:
    """Return True if *path* is the filesystem root or the current user's home directory."""
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return False
    home = Path.home().resolve()
    root = Path("/").resolve()
    return resolved == home or resolved == root


def is_dir_allowed(path: Path) -> bool:
    """Return True if *path* is in the allow list."""
    try:
        resolved = str(path.expanduser().resolve())
    except OSError:
        return False
    return resolved in load_allowed_dirs()


def add_allowed_dir(path: Path) -> None:
    """Add *path* to the allow list (no-op if already present)."""
    resolved = str(path.expanduser().resolve())
    dirs = load_allowed_dirs()
    if resolved not in dirs:
        dirs.append(resolved)
        save_allowed_dirs(dirs)


def remove_allowed_dir(path: Path) -> bool:
    """Remove *path* from the allow list. Returns True if it was present."""
    resolved = str(path.expanduser().resolve())
    dirs = load_allowed_dirs()
    if resolved in dirs:
        dirs.remove(resolved)
        save_allowed_dirs(dirs)
        return True
    return False
