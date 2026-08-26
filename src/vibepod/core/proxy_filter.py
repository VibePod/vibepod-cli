"""Proxy allow/deny filter: validation, config mutation, materialization."""

from __future__ import annotations

import io
import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from vibepod.core.config import (
    _default_config,
    _load_yaml,
    deep_merge,
    get_global_config_path,
    load_project_config,
)
from vibepod.core.profiles import DEFAULT_PROFILE, profiles_root, validate_profile_name
from vibepod.core.proxy_identity import validate_policy_id

POLICY_SCHEMA = 2
VALID_MODES = ("open", "allow", "deny")

# A launch writes its container policy record before its container exists, so a
# concurrent cleanup must not delete a record younger than this window or it
# would strand the still-starting agent with no policy (failing closed).
_ORPHAN_GRACE_SECONDS = 60.0

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
    if not isinstance(proxy_cfg, dict):
        raise ValueError("Config key 'proxy' must be a mapping")
    filter_cfg = proxy_cfg.get("filter", {})
    if not isinstance(filter_cfg, dict):
        raise ValueError("Config key 'proxy.filter' must be a mapping")

    raw_mode = filter_cfg.get("mode", "open")
    if not isinstance(raw_mode, str):
        raise ValueError("Config key 'proxy.filter.mode' must be a string")
    mode = raw_mode.strip().lower()
    if mode not in VALID_MODES:
        raise ValueError(
            f"Invalid proxy.filter.mode '{raw_mode}'. Choose one of: {', '.join(VALID_MODES)}",
        )

    def _patterns(raw: Any, key: str) -> list[str]:
        if not isinstance(raw, list):
            raise ValueError(f"Config key 'proxy.filter.{key}' must be a list of strings")
        if any(not isinstance(pattern, str) for pattern in raw):
            raise ValueError(f"Config key 'proxy.filter.{key}' must contain only strings")
        return [normalize_pattern(pattern) for pattern in raw]

    return {
        "mode": mode,
        "allow": _patterns(filter_cfg.get("allow", []), "allow"),
        "deny": _patterns(filter_cfg.get("deny", []), "deny"),
    }


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


def profile_filter_path(profile: str) -> Path | None:
    """Per-profile filter file; None for the default profile (uses the global config)."""
    if profile == DEFAULT_PROFILE:
        return None
    return profiles_root() / profile / "filter.yaml"


def _load_profile_filter(profile: str) -> dict[str, Any] | None:
    """Return the profile's raw filter mapping, or None when it has no own filter."""
    path = profile_filter_path(profile)
    if path is None or not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Profile filter at {path} must be a YAML mapping")
    return data


def effective_filter_settings(
    config: dict[str, Any],
    profile: str = DEFAULT_PROFILE,
) -> dict[str, Any]:
    """Filter settings for *profile*: its filter.yaml when present, else the config.

    An explicit VP_PROXY_FILTER_MODE always wins, matching its precedence over
    the config files.
    """
    override = _load_profile_filter(profile)
    if override is None:
        return get_filter_settings(config)
    settings = get_filter_settings({"proxy": {"filter": override}})
    env_mode = os.environ.get("VP_PROXY_FILTER_MODE")
    if env_mode is not None:
        settings["mode"] = _normalize_mode(env_mode, "VP_PROXY_FILTER_MODE")
    return settings


def update_profile_filter(
    profile: str,
    mutate: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    """Apply *mutate* to the profile's filter settings and save them.

    The default profile keeps its settings in the global config.yaml; named
    profiles get a filter.yaml in their profile dir, seeded from the global
    settings on first write.
    """
    path = profile_filter_path(profile)
    if path is None:
        return update_global_filter(mutate)
    data: dict[str, Any]
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if raw is not None and not isinstance(raw, dict):
            raise ValueError(
                f"Profile filter at {path} is not a YAML mapping; refusing to rewrite it",
            )
        data = raw if isinstance(raw, dict) else {}
    else:
        data = get_filter_settings(_load_yaml(get_global_config_path()))
    mutate(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, yaml.safe_dump(data, sort_keys=False))
    return data


def _normalize_mode(raw: Any, source: str = "proxy.filter.mode") -> str:
    if not isinstance(raw, str):
        raise ValueError(f"{source} must be a string")
    mode = raw.strip().lower()
    if mode not in VALID_MODES:
        raise ValueError(f"Invalid {source} '{raw}'. Choose one of: {', '.join(VALID_MODES)}")
    return mode


def _normalize_filter_mapping(raw: Any) -> dict[str, Any]:
    """Validate a partial filter mapping while retaining only its explicit keys."""
    if not isinstance(raw, dict):
        raise ValueError("Config key 'proxy.filter' must be a mapping")
    unsupported = set(raw) - {"mode", "allow", "deny"}
    if unsupported:
        names = ", ".join(sorted(str(key) for key in unsupported))
        raise ValueError(f"Unsupported proxy.filter key(s): {names}")
    normalized = get_filter_settings({"proxy": {"filter": raw}})
    return {key: normalized[key] for key in ("mode", "allow", "deny") if key in raw}


def get_proxy_data_dir(config: dict[str, Any]) -> Path:
    """Return the host directory mounted at ``/data`` in the proxy container."""
    return get_filter_file_path(config).parent


def materialized_profile_path(config: dict[str, Any], profile: str) -> Path:
    validate_profile_name(profile)
    return get_proxy_data_dir(config) / "policies" / "profiles" / f"{profile}.json"


def container_policy_path(config: dict[str, Any], policy_id: str) -> Path:
    return (
        get_proxy_data_dir(config)
        / "policies"
        / "containers"
        / f"{validate_policy_id(policy_id)}.json"
    )


def _global_filter_settings() -> dict[str, Any]:
    global_config = deep_merge(_default_config(), _load_yaml(get_global_config_path()))
    return get_filter_settings(global_config)


def materialize_policy_bases(config: dict[str, Any], profile: str) -> list[Path]:
    """Atomically materialize the global and selected explicit-profile policy bases."""
    global_path = get_filter_file_path(config)
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_document = {"version": POLICY_SCHEMA, **_global_filter_settings()}
    _atomic_write_text(global_path, json.dumps(global_document, indent=2) + "\n")
    written = [global_path]

    if profile == DEFAULT_PROFILE:
        return written

    target = materialized_profile_path(config, profile)
    profile_filter = _load_profile_filter(profile)
    if profile_filter is None:
        # No source filter.yaml: leave any existing materialized base in place.
        # A still-running container may reference it, and reference-aware
        # deletion is cleanup_orphan_policies' job, not this hot path's.
        return written

    target.parent.mkdir(parents=True, exist_ok=True)
    profile_document = {
        "version": POLICY_SCHEMA,
        "profile": profile,
        **get_filter_settings({"proxy": {"filter": profile_filter}}),
    }
    _atomic_write_text(target, json.dumps(profile_document, indent=2) + "\n")
    written.append(target)
    return written


def _project_filter(workspace: Path) -> dict[str, Any] | None:
    project = load_project_config(workspace)
    if "proxy" not in project:
        return None
    proxy = project["proxy"]
    if not isinstance(proxy, dict):
        raise ValueError("Project config key 'proxy' must be a mapping")
    if "filter" not in proxy:
        return None
    return _normalize_filter_mapping(proxy["filter"])


def materialize_container_policy(
    config: dict[str, Any],
    *,
    profile: str,
    workspace: Path,
    policy_id: str,
) -> Path:
    """Capture launch-specific project and environment policy inputs."""
    validated_id = validate_policy_id(policy_id)
    env_value = os.environ.get("VP_PROXY_FILTER_MODE")
    env_mode = _normalize_mode(env_value, "VP_PROXY_FILTER_MODE") if env_value is not None else None
    document = {
        "version": POLICY_SCHEMA,
        "policy_id": validated_id,
        "profile": profile,
        "project_filter": _project_filter(workspace),
        "env_mode": env_mode,
    }
    path = container_policy_path(config, validated_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, json.dumps(document, indent=2) + "\n")
    return path


def _load_policy_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to read proxy policy at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Proxy policy at {path} must be a JSON object")
    if data.get("version") != POLICY_SCHEMA:
        raise ValueError(f"Proxy policy at {path} does not use schema {POLICY_SCHEMA}")
    return data


def _settings_from_document(document: dict[str, Any]) -> dict[str, Any]:
    raw = {
        key: document.get(key, default)
        for key, default in (
            ("mode", "open"),
            ("allow", []),
            ("deny", []),
        )
    }
    return get_filter_settings({"proxy": {"filter": raw}})


def resolve_container_policy(config: dict[str, Any], policy_id: str) -> dict[str, Any]:
    """Resolve the live effective settings for one materialized launch policy."""
    record = _load_policy_json(container_policy_path(config, policy_id))
    if record.get("policy_id") != policy_id:
        raise ValueError("Container proxy policy id does not match its filename")
    profile = record.get("profile")
    if not isinstance(profile, str) or not profile:
        raise ValueError("Container proxy policy profile must be a non-empty string")
    validate_profile_name(profile)

    global_settings = _settings_from_document(_load_policy_json(get_filter_file_path(config)))
    profile_path = materialized_profile_path(config, profile)
    if profile != DEFAULT_PROFILE and profile_path.exists():
        settings = _settings_from_document(_load_policy_json(profile_path))
    else:
        merged = dict(global_settings)
        project_filter = record.get("project_filter")
        if project_filter is not None:
            merged.update(_normalize_filter_mapping(project_filter))
        settings = get_filter_settings({"proxy": {"filter": merged}})

    env_mode = record.get("env_mode")
    if env_mode is not None:
        settings["mode"] = _normalize_mode(env_mode, "container env_mode")
    return settings


def remove_container_policy(config: dict[str, Any], policy_id: str) -> None:
    """Remove one launch policy record if it exists."""
    container_policy_path(config, policy_id).unlink(missing_ok=True)


def cleanup_orphan_policies(
    config: dict[str, Any],
    referenced_policy_ids: set[str],
) -> dict[str, int]:
    """Remove records not referenced by any existing managed container."""
    root = get_proxy_data_dir(config) / "policies"
    containers_dir = root / "containers"
    profiles_dir = root / "profiles"
    removed_containers = 0
    referenced_profiles: set[str] = set()
    unknown_reference = False
    now = time.time()

    if containers_dir.exists():
        for path in containers_dir.glob("*.json"):
            policy_id = path.stem
            keep = policy_id in referenced_policy_ids
            if not keep:
                try:
                    age = now - path.stat().st_mtime
                except OSError:
                    continue
                # A record younger than the grace window may belong to a
                # concurrent launch whose container has not been created yet.
                keep = age < _ORPHAN_GRACE_SECONDS
            if not keep:
                path.unlink(missing_ok=True)
                removed_containers += 1
                continue
            try:
                record = _load_policy_json(path)
            except ValueError:
                unknown_reference = True
                continue
            profile = record.get("profile")
            if isinstance(profile, str) and profile:
                referenced_profiles.add(profile)
            else:
                unknown_reference = True

    removed_profiles = 0
    if profiles_dir.exists() and not unknown_reference:
        for path in profiles_dir.glob("*.json"):
            profile = path.stem
            try:
                validate_profile_name(profile)
            except ValueError:
                continue
            source = profile_filter_path(profile)
            if profile not in referenced_profiles and (source is None or not source.exists()):
                path.unlink(missing_ok=True)
                removed_profiles += 1

    return {"containers": removed_containers, "profiles": removed_profiles}


def _roundtrip_yaml() -> YAML:
    """A round-trip YAML handler that keeps comments, anchors, and quoting."""
    handler = YAML()
    handler.preserve_quotes = True
    return handler


def update_global_filter(mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Apply *mutate* to proxy.filter in the global config.yaml and save it.

    The file is edited via a round-trip loader so a user's comments, anchors,
    and formatting survive a filter change.
    """
    path = get_global_config_path()
    handler = _roundtrip_yaml()
    data: Any = None
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if text.strip():
            data = handler.load(text)
            if not isinstance(data, dict):
                raise ValueError(
                    f"Global config at {path} is not a YAML mapping; refusing to rewrite it",
                )
    if data is None:
        data = CommentedMap()
    proxy_cfg = data.setdefault("proxy", CommentedMap())
    if not isinstance(proxy_cfg, dict):
        raise ValueError("Config key 'proxy' must be a mapping")
    filter_cfg = proxy_cfg.setdefault("filter", CommentedMap())
    if not isinstance(filter_cfg, dict):
        raise ValueError("Config key 'proxy.filter' must be a mapping")
    mutate(filter_cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO()
    handler.dump(data, buffer)
    _atomic_write_text(path, buffer.getvalue())
    return filter_cfg
