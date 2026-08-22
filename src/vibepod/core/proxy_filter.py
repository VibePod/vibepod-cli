"""Proxy allow/deny filter: validation, config mutation, materialization."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from vibepod.core.config import _load_yaml, get_global_config_path
from vibepod.utils.console import warning

VALID_MODES = ("open", "allow", "deny")

_PATTERN_RE = re.compile(
    r"^(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$",
)


def normalize_pattern(raw: str) -> str:
    """Validate and normalize a host pattern; raise ValueError if invalid."""
    pattern = raw.strip().lower().rstrip(".")
    if not _PATTERN_RE.match(pattern):
        raise ValueError(
            f"Invalid host pattern '{raw}'. Use a hostname like 'example.com' "
            "or a subdomain wildcard like '*.example.com'.",
        )
    return pattern


def get_filter_settings(config: dict[str, Any]) -> dict[str, Any]:
    """Return normalized filter settings from an effective config."""
    proxy_cfg = config.get("proxy", {})
    filter_cfg = proxy_cfg.get("filter", {}) if isinstance(proxy_cfg, dict) else {}
    if not isinstance(filter_cfg, dict):
        filter_cfg = {}

    mode = str(filter_cfg.get("mode", "open")).strip().lower()
    if mode not in VALID_MODES:
        mode = "open"

    def _patterns(raw: Any) -> list[str]:
        if not isinstance(raw, list):
            return []
        return [str(p).strip().lower().rstrip(".") for p in raw if str(p).strip()]

    return {
        "mode": mode,
        "allow": _patterns(filter_cfg.get("allow")),
        "deny": _patterns(filter_cfg.get("deny")),
    }


def raw_configured_mode(config: dict[str, Any]) -> str:
    """Return the configured mode as written, before fail-open coercion."""
    proxy_cfg = config.get("proxy", {})
    filter_cfg = proxy_cfg.get("filter", {}) if isinstance(proxy_cfg, dict) else {}
    if not isinstance(filter_cfg, dict):
        return "open"
    return str(filter_cfg.get("mode", "open"))


def get_filter_file_path(config: dict[str, Any]) -> Path:
    proxy_cfg = config.get("proxy", {})
    db_path = (
        Path(str(proxy_cfg.get("db_path", "~/.config/vibepod/proxy/proxy.db")))
        .expanduser()
        .resolve()
    )
    return db_path.parent / "filter.json"


def _atomic_write_text(path: Path, content: str) -> None:
    """Write via a sibling temp file + rename so readers never see partials."""
    # Follow symlinks: replacing the link itself would disconnect e.g. a
    # dotfiles-managed config.yaml from its target.
    path = path.resolve()
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        os.unlink(tmp_name)
        raise


def write_filter_file(config: dict[str, Any]) -> Path:
    """Materialize filter settings into the proxy data dir (hot-reloaded by the proxy)."""
    raw_mode = raw_configured_mode(config)
    if raw_mode.strip().lower() not in VALID_MODES:
        warning(f"Invalid proxy.filter.mode '{raw_mode}' in config; treating as 'open'")
    path = get_filter_file_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, json.dumps(get_filter_settings(config), indent=2) + "\n")
    return path


def update_global_filter(mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Apply *mutate* to proxy.filter in the global config.yaml and save it."""
    path = get_global_config_path()
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if raw is not None and not isinstance(raw, dict):
            raise ValueError(
                f"Global config at {path} is not a YAML mapping; refusing to rewrite it",
            )
    data = _load_yaml(path)
    proxy_cfg = data.setdefault("proxy", {})
    if not isinstance(proxy_cfg, dict):
        raise ValueError("Config key 'proxy' must be a mapping")
    filter_cfg = proxy_cfg.setdefault("filter", {})
    if not isinstance(filter_cfg, dict):
        raise ValueError("Config key 'proxy.filter' must be a mapping")
    mutate(filter_cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, yaml.safe_dump(data, sort_keys=False))
    return filter_cfg
