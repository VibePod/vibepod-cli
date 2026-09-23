"""User-global provider metadata and owner-only credential storage.

No agent configuration is modified by this module. Secrets are resolved at use,
never included in provider metadata or model caches.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

PROTOCOLS = ("openai-chat", "openai-responses", "anthropic")
#: Portable reasoning levels; each adapter maps them to its agent's native names.
REASONING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh")


@dataclass(frozen=True)
class ModelSettings:
    """Optional per-model limits and reasoning controls. Unset fields stay unset."""

    context_window: int | None = None
    max_output_tokens: int | None = None
    reasoning: bool | None = None
    reasoning_levels: tuple[str, ...] = ()
    reasoning_default: str = ""

    def validate(self, model: str) -> None:
        for label, value in (
            ("context_window", self.context_window),
            ("max_output_tokens", self.max_output_tokens),
        ):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError(f"{label} for model '{model}' must be a positive integer")
            if value is not None and value <= 0:
                raise ValueError(f"{label} for model '{model}' must be a positive integer")
        if self.reasoning is not None and not isinstance(self.reasoning, bool):
            raise ValueError(f"reasoning for model '{model}' must be true or false")
        levels = self.reasoning_levels
        if (
            not isinstance(levels, tuple)
            or any(level not in REASONING_LEVELS for level in levels)
            or len(set(levels)) != len(levels)
        ):
            raise ValueError(
                "Reasoning levels must be distinct values from: " + ", ".join(REASONING_LEVELS),
            )
        if (levels or self.reasoning_default) and self.reasoning is False:
            raise ValueError(
                f"Model '{model}' is marked non-reasoning; remove its reasoning levels",
            )
        if self.reasoning_default:
            allowed = levels or REASONING_LEVELS
            if self.reasoning_default not in allowed:
                raise ValueError(
                    f"Default reasoning level for model '{model}' must be one of: "
                    + ", ".join(allowed),
                )

    def is_empty(self) -> bool:
        return self == ModelSettings()

    @property
    def effective_reasoning(self) -> bool | None:
        """Levels or a default level imply a reasoning model unless explicitly denied."""
        if self.reasoning is None and (self.reasoning_levels or self.reasoning_default):
            return True
        return self.reasoning


@dataclass(frozen=True)
class Provider:
    name: str
    protocol: str
    base_url: str
    auth: str = "none"
    key_env: str = ""
    models: tuple[str, ...] = ()
    default_model: str = ""
    credential_file: str = "credentials.json"
    model_settings: dict[str, ModelSettings] = field(default_factory=dict)

    def validate(self) -> None:
        validate_name(self.name)
        if not re.fullmatch(r"credentials(?:-[a-f0-9]{32})?\.json", self.credential_file):
            raise ValueError("Invalid credential file reference")
        if self.protocol not in PROTOCOLS:
            raise ValueError(f"Protocol must be one of: {', '.join(PROTOCOLS)}")
        validate_url(self.base_url)
        if self.protocol == "anthropic" and urlsplit(self.base_url).path.rstrip("/").endswith(
            "/v1",
        ):
            raise ValueError(
                "Anthropic endpoints must not end with /v1; clients append /v1 themselves",
            )
        if self.auth not in ("none", "key", "env"):
            raise ValueError("Authentication must be none, key, or env")
        if self.auth == "env" and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.key_env):
            raise ValueError("An environment variable name is required")
        if self.auth != "env" and self.key_env:
            raise ValueError("Environment variable only applies to env authentication")
        if any(not valid_model_id(m) for m in self.models):
            raise ValueError("Model IDs must be nonempty strings")
        if self.default_model and self.default_model not in self.models:
            raise ValueError("Default model must be in the selected models")
        if not isinstance(self.model_settings, dict):
            raise ValueError("Invalid model settings")
        for model, settings in self.model_settings.items():
            if model not in self.models:
                raise ValueError(f"Settings refer to unselected model '{model}'")
            if not isinstance(settings, ModelSettings):
                raise ValueError("Invalid model settings")
            settings.validate(model)


def valid_model_id(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value.isprintable()


def validate_key(key: str) -> None:
    if any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError("API key must contain only visible ASCII characters")


def validate_name(name: str) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
        raise ValueError("Provider name must be a lowercase slug (1–64 characters)")


def validate_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme in ("http", "https")
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and not any(c.isspace() or not c.isprintable() for c in url)
        )
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Endpoint must be an HTTP(S) URL without credentials, query, or fragment")


def provider_root() -> Path:
    return Path(os.environ.get("VP_PROVIDERS_DIR", str(Path.home() / ".vibepod/providers")))


def _safe_path(path: Path) -> None:
    """Refuse symlinks at or below the store root; ancestors above it are the user's."""
    root = provider_root()
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError("Symlink paths are not allowed in the provider store")
        if component == root:
            break


def _private(path: Path) -> None:
    _safe_path(path)
    if not hasattr(os, "getuid"):
        raise ValueError("Provider storage requires owner-only POSIX permissions on this platform")
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        mode = "700" if stat.S_ISDIR(info.st_mode) else "600"
        raise ValueError(
            "Provider store permissions must restrict access to the current user: "
            f"run `chmod {mode} {path}` (and check its owner)",
        )


def _directory(name: str) -> Path:
    validate_name(name)
    path = provider_root() / name
    _safe_path(path)
    if not path.is_dir():
        raise ValueError(f"Provider '{name}' does not exist; use `vp provider add`")
    _private(path.parent)
    _private(path)
    return path


def _read_json(path: Path) -> Any:
    _private(path)
    try:
        return json.loads(path.read_text())
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid provider JSON file") from exc


def _write_json(path: Path, value: Any) -> None:
    _safe_path(path)
    fd, filename = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    temporary = Path(filename)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _models_payload(models: list[str]) -> dict[str, Any]:
    if any(not valid_model_id(m) for m in models):
        raise ValueError("Model IDs must be nonempty strings")
    return {
        "models": sorted(set(models)),
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
    }


def save_provider(
    provider: Provider,
    *,
    key: str = "",
    discovered: list[str] | None = None,
) -> None:
    """Create a provider atomically; never overwrite an existing identity.

    ``discovered`` seeds the model cache inside the same staged directory, so a
    failed cache write leaves no half-created provider behind.
    """
    provider.validate()
    validate_key(key)
    if provider.auth == "key" and not key.strip():
        raise ValueError("An API key is required for stored-key authentication")
    if provider.auth != "key" and key:
        raise ValueError("An API key may only be saved with stored-key authentication")
    root = provider_root()
    _safe_path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _private(root)
    destination = root / provider.name
    _safe_path(destination)
    if destination.exists():
        raise ValueError(f"Provider '{provider.name}' already exists")
    temporary = Path(tempfile.mkdtemp(dir=root, prefix=".create-"))
    try:
        _write_metadata(temporary / "provider.toml", provider)
        if provider.auth == "key":
            _write_json(temporary / provider.credential_file, {"api_key": key})
        if discovered is not None:
            _write_json(temporary / "models.json", _models_payload(discovered))
        try:
            temporary.rename(destination)
        except OSError as exc:
            if destination.exists():
                raise ValueError(f"Provider '{provider.name}' already exists") from exc
            raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _render_metadata(provider: Provider) -> str:
    """TOML text: scalar fields as JSON literals (valid TOML), settings as tables."""
    data = {"version": 1, **asdict(provider)}
    settings = data.pop("model_settings")
    lines = [f"{name} = {json.dumps(value, ensure_ascii=False)}" for name, value in data.items()]
    for model, entry in settings.items():
        values = {k: v for k, v in entry.items() if v is not None and v != "" and v != ()}
        if not values:
            continue
        lines += ["", f"[model_settings.{json.dumps(model, ensure_ascii=False)}]"]
        lines += [f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in values.items()]
    return "\n".join(lines) + "\n"


def _write_metadata(path: Path, provider: Provider) -> None:
    """Publish one complete metadata snapshot, with owner-only permissions."""
    _safe_path(path)
    fd, filename = tempfile.mkstemp(dir=path.parent, prefix=".metadata-")
    temporary = Path(filename)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(_render_metadata(provider))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _settings_from_raw(entry: object) -> ModelSettings:
    if not isinstance(entry, dict):
        raise ValueError("Invalid model settings")
    values = dict(entry)
    levels = values.get("reasoning_levels", ())
    if not isinstance(levels, list | tuple):
        raise ValueError("Invalid model settings")
    values["reasoning_levels"] = tuple(levels)
    return ModelSettings(**values)


def update_provider(provider: Provider, *, key: str | None = None) -> None:
    """Update settings; None preserves a stored key, never exposes it in metadata.

    Key rotation writes a new private credential file before atomically publishing
    its reference. A failed metadata write therefore cannot destroy the old key.
    """
    provider.validate()
    previous = load_provider(provider.name)
    directory = _directory(provider.name)
    old_key_path = directory / previous.credential_file
    _safe_path(old_key_path)
    new_key_path: Path | None = None
    if provider.auth == "key":
        if key is None:
            if previous.auth != "key":
                raise ValueError(
                    "An API key is required when switching to stored-key authentication",
                )
            resolve_key(previous)
            provider = replace(provider, credential_file=previous.credential_file)
        else:
            validate_key(key)
            if not key:
                raise ValueError("An API key is required for stored-key authentication")
            provider = replace(provider, credential_file=f"credentials-{uuid4().hex}.json")
            new_key_path = directory / provider.credential_file
    elif key:
        raise ValueError("An API key may only be saved with stored-key authentication")
    else:
        provider = replace(provider, credential_file="credentials.json")
    try:
        if new_key_path is not None:
            _write_json(new_key_path, {"api_key": key})
        _write_metadata(directory / "provider.toml", provider)
    except BaseException:
        if new_key_path is not None:
            new_key_path.unlink(missing_ok=True)
        raise
    if previous.auth == "key" and (
        provider.auth != "key" or previous.credential_file != provider.credential_file
    ):
        old_key_path.unlink(missing_ok=True)


def load_provider(name: str) -> Provider:
    path = _directory(name) / "provider.toml"
    _private(path)
    try:
        data = tomllib.loads(path.read_text())
        if data.pop("version", None) != 1 or data.get("name") != name:
            raise ValueError("Unsupported provider metadata version or identity")
        # Pre-release metadata carried a separate container-facing URL; one URL
        # reachable from host and container replaced it (see docs, Local providers).
        data.pop("runtime_url", None)
        raw_models = data.get("models", [])
        if not isinstance(raw_models, list):
            raise ValueError("Invalid selected models")
        data["models"] = tuple(raw_models)
        raw_settings = data.pop("model_settings", {})
        if not isinstance(raw_settings, dict):
            raise ValueError("Invalid model settings")
        data["model_settings"] = {
            str(model): _settings_from_raw(entry) for model, entry in raw_settings.items()
        }
        p = Provider(**data)
        p.validate()
        return p
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"Invalid metadata for provider '{name}'") from exc


def resolve_key(provider: Provider) -> str:
    if provider.auth == "none":
        return ""
    if provider.auth == "env":
        value = os.environ.get(provider.key_env, "")
        if not value:
            raise ValueError(
                f"Set environment variable {provider.key_env} before using this provider",
            )
        validate_key(value)
        return value
    provider.validate()
    data = _read_json(_directory(provider.name) / provider.credential_file)
    key = data.get("api_key") if isinstance(data, dict) else None
    if not isinstance(key, str) or not key.strip():
        raise ValueError("Stored provider key is missing or invalid")
    validate_key(key)
    return key


def list_providers() -> list[str]:
    root = provider_root()
    _safe_path(root)
    if not root.exists():
        return []
    _private(root)
    return sorted(p.name for p in root.iterdir() if not p.name.startswith(".") and p.is_dir())


def save_models(name: str, models: list[str]) -> None:
    _write_json(_directory(name) / "models.json", _models_payload(models))


def load_models(name: str) -> list[str]:
    path = _directory(name) / "models.json"
    if not path.exists():
        return list(load_provider(name).models)
    data = _read_json(path)
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list) or any(not valid_model_id(m) for m in models):
        raise ValueError("Invalid model cache")
    return models


def remove_provider(name: str) -> None:
    shutil.rmtree(_directory(name))
