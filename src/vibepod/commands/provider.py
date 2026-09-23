"""Interactive setup for global hosted and local model providers."""

from __future__ import annotations

from dataclasses import replace
from typing import Annotated
from urllib.parse import urlsplit

import typer

from vibepod.core.provider_discovery import discover_models
from vibepod.core.providers import (
    PROTOCOLS,
    REASONING_LEVELS,
    ModelSettings,
    Provider,
    list_providers,
    load_models,
    load_provider,
    remove_provider,
    resolve_key,
    save_models,
    save_provider,
    update_provider,
    validate_name,
    validate_url,
)
from vibepod.utils.console import console, error, success, warning

app = typer.Typer(help="Manage global model providers", no_args_is_help=True)


def _confirm_discovery(provider: Provider, key: str, *, abort: bool = True) -> bool | None:
    """Show the destination and ask before contacting it.

    Returns whether a key may travel over HTTP, or ``None`` when the user
    declines and ``abort`` is off (the wizards then fall back to manual entry).
    Discovery-only commands keep ``abort`` on: declining ends them.
    """
    console.print(f"Discovery destination: {provider.base_url}", markup=False)
    if key and urlsplit(provider.base_url).scheme == "http":
        if typer.confirm("Send your API key over unencrypted HTTP?", default=False, abort=abort):
            return True
        return None
    if typer.confirm("Contact this endpoint to list models?", default=True, abort=abort):
        return False
    return None


def _validate_protocol(protocol: str) -> None:
    if protocol not in PROTOCOLS:
        raise ValueError(f"Protocol must be one of: {', '.join(PROTOCOLS)}")


def _validate_auth(auth: str) -> None:
    if auth not in ("none", "key", "env"):
        raise ValueError("Authentication must be none, key, or env")


def _select_models(
    discovered: list[str] | None,
    *,
    previous: Provider | None = None,
) -> tuple[tuple[str, ...], str]:
    """Pick the selected models and default: all discovered, or a typed list.

    With ``previous`` (editing) the typed list defaults to the saved selection,
    the default prompt keeps the saved default while still selected, and ``-``
    clears it.
    """
    if discovered and typer.confirm("Use all discovered models?", default=True):
        selected = tuple(discovered)
    else:
        if previous is None:
            typed = typer.prompt("Model IDs (comma-separated)")
        else:
            typed = typer.prompt("Model IDs (comma-separated)", default=", ".join(previous.models))
        selected = tuple(dict.fromkeys(m.strip() for m in typed.split(",") if m.strip()))
    if not selected:
        raise ValueError("Select at least one model")
    if previous is None:
        return selected, typer.prompt("Default model (empty keeps native selection)", default="")
    default = typer.prompt(
        "Default model (- clears selection)",
        default=previous.default_model if previous.default_model in selected else "",
    )
    return selected, "" if default == "-" else default


def _prompt_settings(model: str, current: ModelSettings) -> ModelSettings:
    """Prompt for one model's limits and reasoning controls; Enter keeps the shown value."""

    def number(label: str, value: int | None) -> int | None:
        raw = typer.prompt(
            f"{label} for {model} (tokens, empty = unknown)",
            default=str(value or ""),
        )
        raw = raw.strip()
        if not raw:
            return None
        if not raw.isdigit() or int(raw) <= 0:
            raise ValueError(f"{label} must be a positive integer")
        return int(raw)

    context_window = number("Context window", current.context_window)
    max_output_tokens = number("Max output tokens", current.max_output_tokens)
    reasoning = typer.confirm(
        f"Is {model} a reasoning model?",
        default=bool(current.effective_reasoning),
    )
    levels: tuple[str, ...] = ()
    default_level = ""
    if reasoning:
        raw_levels = typer.prompt(
            "Reasoning levels (comma-separated from "
            + ", ".join(REASONING_LEVELS)
            + "; empty = all)",
            default=",".join(current.reasoning_levels),
        )
        levels = tuple(
            dict.fromkeys(level.strip() for level in raw_levels.split(",") if level.strip()),
        )
        default_level = typer.prompt(
            "Default reasoning level (empty = agent default)",
            default=current.reasoning_default,
        ).strip()
    settings = ModelSettings(
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        reasoning=reasoning,
        reasoning_levels=levels,
        reasoning_default=default_level,
    )
    settings.validate(model)
    return settings


def _configure_settings(
    selected: tuple[str, ...],
    default: str,
    existing: dict[str, ModelSettings],
) -> dict[str, ModelSettings]:
    """Optionally walk selected models; settings for dropped models are discarded."""
    settings = {model: value for model, value in existing.items() if model in selected}
    if not typer.confirm(
        "Configure context window, output limit, or reasoning for models now?",
        default=False,
    ):
        return settings
    hint = default or selected[0]
    raw = typer.prompt(f"Models to configure (comma-separated, empty = {hint})", default="").strip()
    targets = [m.strip() for m in raw.split(",") if m.strip()] or [hint]
    for target in dict.fromkeys(targets):
        if target not in selected:
            raise ValueError(f"Model '{target}' is not selected")
        settings[target] = _prompt_settings(target, settings.get(target, ModelSettings()))
    return {model: value for model, value in settings.items() if not value.is_empty()}


def _failure(exc: ValueError | OSError) -> None:
    # Filesystem errors can contain paths, not file contents. Avoid raw HTTP bodies.
    error(str(exc) if isinstance(exc, ValueError) else "Cannot access the provider store")
    raise typer.Exit(1) from exc


@app.command("add")
def add() -> None:
    """Configure an endpoint, credentials, and selected models interactively."""
    try:
        name = typer.prompt("Provider name")
        validate_name(name)
        if name in list_providers():
            raise ValueError(f"Provider '{name}' already exists")
        protocol = typer.prompt(f"API protocol ({', '.join(PROTOCOLS)})", default="openai-chat")
        _validate_protocol(protocol)
        base_url = typer.prompt("API base URL")
        validate_url(base_url)
        if urlsplit(base_url).hostname in ("localhost", "127.0.0.1", "::1"):
            warning(
                "Agents run in containers, where localhost is the container itself. "
                "Use a LAN IP, host.docker.internal, or a container name on the "
                "VibePod network (see the Local providers documentation).",
            )
        auth = typer.prompt("Authentication (none, key, env)", default="none")
        _validate_auth(auth)
        key = ""
        key_env = ""
        if auth == "key":
            key = typer.prompt("API key", hide_input=True)
            typer.confirm(
                "Store this key as plaintext in an owner-only file?",
                default=False,
                abort=True,
            )
        elif auth == "env":
            key_env = typer.prompt("API key environment variable")
        p = Provider(name, protocol, base_url, auth, key_env)
        p.validate()
        discovered: list[str] = []
        if typer.confirm("Discover models now?", default=True):
            try:
                discovery_key = key if auth == "key" else resolve_key(p)
                allow_http = _confirm_discovery(p, discovery_key, abort=False)
                if allow_http is None:
                    raise ValueError("Discovery skipped")
                discovered = discover_models(p, key=discovery_key, allow_http_key=allow_http)
                for model in discovered:
                    console.print(model, markup=False)
            except ValueError as exc:
                warning(f"{exc}. You can enter model IDs manually.")
        selected, default = _select_models(discovered)
        settings = _configure_settings(selected, default, {})
        p = Provider(name, protocol, base_url, auth, key_env, selected, default)
        p = replace(p, model_settings=settings)
        save_provider(p, key=key, discovered=discovered or None)
        success(f"Saved provider '{name}'. Native agent configuration is unchanged.")
    except (ValueError, OSError) as exc:
        _failure(exc)


@app.command("edit")
def edit(name: Annotated[str, typer.Argument(help="Provider name")]) -> None:
    """Edit settings and optionally refresh/select models, without exposing saved keys."""
    try:
        previous = load_provider(name)
        protocol = typer.prompt(f"API protocol ({', '.join(PROTOCOLS)})", default=previous.protocol)
        _validate_protocol(protocol)
        base_url = typer.prompt("API base URL", default=previous.base_url)
        auth = typer.prompt("Authentication (none, key, env)", default=previous.auth)
        _validate_auth(auth)
        key: str | None = None
        key_env = ""
        if auth == "key":
            keep = previous.auth == "key" and typer.confirm(
                "Keep the stored API key?",
                default=True,
            )
            if not keep:
                key = typer.prompt("API key", hide_input=True)
                typer.confirm(
                    "Store this key as plaintext in an owner-only file?",
                    default=False,
                    abort=True,
                )
        elif auth == "env":
            key_env = typer.prompt("API key environment variable", default=previous.key_env)
        p = replace(
            previous,
            protocol=protocol,
            base_url=base_url,
            auth=auth,
            key_env=key_env,
        )
        p.validate()
        discovered: list[str] | None = None
        if typer.confirm("Refresh models now?", default=True):
            try:
                discovery_key = key if key is not None else resolve_key(p)
                allow_http = _confirm_discovery(p, discovery_key, abort=False)
                if allow_http is None:
                    raise ValueError("Discovery skipped")
                discovered = discover_models(p, key=discovery_key, allow_http_key=allow_http)
                for model in discovered:
                    console.print(model, markup=False)
            except ValueError as exc:
                warning(f"{exc}. Existing selections are available for manual editing.")
        selected, default = _select_models(discovered, previous=previous)
        settings = _configure_settings(selected, default, previous.model_settings)
        p = replace(p, models=selected, default_model=default, model_settings=settings)
        # Cache first: it is informational, so a failed metadata write leaves the
        # selection untouched and the cache truthfully newer, never the reverse.
        if discovered is not None:
            save_models(name, discovered)
        update_provider(p, key=key)
        success(f"Updated provider '{name}'. Changes apply to future launches only.")
    except (ValueError, OSError) as exc:
        _failure(exc)


@app.command("list")
def list_() -> None:
    """List provider identities without reading credentials."""
    try:
        names = list_providers()
    except (ValueError, OSError) as exc:
        _failure(exc)
    for name in names:
        try:
            p = load_provider(name)
        except (ValueError, OSError) as exc:
            # One broken entry must not hide the rest; the message never has secrets.
            reason = str(exc) if isinstance(exc, ValueError) else "cannot access the provider store"
            warning(f"{name}  ({reason})")
            continue
        console.print(f"{name}  {p.protocol}  {p.base_url}  auth={p.auth}", markup=False)


@app.command("models")
def models(
    name: Annotated[str, typer.Argument(help="Provider name")],
    refresh: Annotated[bool, typer.Option("--refresh", help="Refresh the model cache")] = False,
) -> None:
    """Show cached model IDs, optionally querying the server again."""
    try:
        p = load_provider(name)
        if refresh:
            key = resolve_key(p)
            allow_http = _confirm_discovery(p, key)
            assert allow_http is not None  # abort=True never returns None
            found = discover_models(p, key=key, allow_http_key=allow_http)
            save_models(name, found)
        for model in load_models(name):
            console.print(model + _settings_summary(p.model_settings.get(model)), markup=False)
    except (ValueError, OSError) as exc:
        if refresh:
            warning("Refresh failed; saved models and selections have not changed.")
        _failure(exc)


def _settings_summary(settings: ModelSettings | None) -> str:
    if settings is None or settings.is_empty():
        return ""
    parts: list[str] = []
    if settings.context_window:
        parts.append(f"context={settings.context_window}")
    if settings.max_output_tokens:
        parts.append(f"max_output={settings.max_output_tokens}")
    if settings.effective_reasoning is not None:
        parts.append(f"reasoning={'yes' if settings.effective_reasoning else 'no'}")
    if settings.reasoning_levels:
        parts.append("levels=" + ",".join(settings.reasoning_levels))
    if settings.reasoning_default:
        parts.append(f"default_level={settings.reasoning_default}")
    return "  " + " ".join(parts)


@app.command("refresh")
def refresh(name: Annotated[str, typer.Argument(help="Provider name")]) -> None:
    """Re-discover models, choose which to use, and set the default; nothing else changes."""
    try:
        previous = load_provider(name)
        key = resolve_key(previous)
        allow_http = _confirm_discovery(previous, key)
        assert allow_http is not None  # abort=True never returns None
        discovered = discover_models(previous, key=key, allow_http_key=allow_http)
        for model in discovered:
            console.print(model, markup=False)
        selected, default = _select_models(discovered, previous=previous)
        settings = {m: s for m, s in previous.model_settings.items() if m in selected}
        save_models(name, discovered)  # cache first, see edit()
        update_provider(
            replace(previous, models=selected, default_model=default, model_settings=settings),
        )
        success(f"Refreshed models for provider '{name}'. Changes apply to future launches.")
    except (ValueError, OSError) as exc:
        _failure(exc)


@app.command("remove")
def remove(
    name: Annotated[str, typer.Argument(help="Provider name")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Remove a global provider, including its stored key."""
    try:
        load_provider(name)
        if not yes:
            typer.confirm(f"Remove '{name}' and its stored credentials?", abort=True)
        remove_provider(name)
        success(f"Removed provider '{name}'")
    except (ValueError, OSError) as exc:
        _failure(exc)
