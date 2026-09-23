"""Native provider plans for Pi, Codex, OpenCode, Tau, and Jcode.

Secrets stay in launch environment; plans reference keys by variable name only.
"""

from __future__ import annotations

import ipaddress
import json
from urllib.parse import urlsplit

from vibepod.core.providers import (
    REASONING_LEVELS,
    ModelSettings,
    Provider,
    load_provider,
    resolve_key,
)

PI_APIS = {
    "openai-chat": "openai-completions",
    "openai-responses": "openai-responses",
    "anthropic": "anthropic-messages",
}
PLAN_ENV = "VIBEPOD_PROVIDER_PLAN"
#: jcode: rendered `[providers.<name>]` sections and the names for the clash
#: check, consumed by the sh bootstrap (no TOML tooling in that image).
JCODE_CONFIG_ENV = "VIBEPOD_PROVIDER_CONFIG_TOML"
JCODE_NAMES_ENV = "VIBEPOD_PROVIDER_NAMES"


def settings_plan(provider: Provider) -> dict[str, dict[str, object]]:
    """Per-model settings for the container bootstraps; only set fields appear."""
    plan: dict[str, dict[str, object]] = {}
    for model, settings in provider.model_settings.items():
        entry: dict[str, object] = {}
        if settings.context_window:
            entry["contextWindow"] = settings.context_window
        if settings.max_output_tokens:
            entry["maxOutputTokens"] = settings.max_output_tokens
        if settings.effective_reasoning is not None:
            entry["reasoning"] = settings.effective_reasoning
        if settings.reasoning_levels:
            entry["reasoningLevels"] = list(settings.reasoning_levels)
        if settings.reasoning_default:
            entry["reasoningDefault"] = settings.reasoning_default
        if entry:
            plan[model] = entry
    return plan


#: Pi thinking levels (pi docs/models.md); "max" has no portable counterpart.
PI_LEVELS = (*REASONING_LEVELS, "max")


def _pi_model(model: str, settings: ModelSettings | None, protocol: str) -> dict[str, object]:
    """Pi models.json entry: contextWindow, maxTokens, reasoning, thinkingLevelMap."""
    entry: dict[str, object] = {"id": model}
    if settings is None:
        return entry
    if settings.context_window:
        entry["contextWindow"] = settings.context_window
    if settings.max_output_tokens:
        entry["maxTokens"] = settings.max_output_tokens
    if settings.effective_reasoning is not None:
        entry["reasoning"] = settings.effective_reasoning
    if settings.reasoning_levels:
        # null hides a level; omitted standard levels use Pi's default wire mapping;
        # xhigh must be mapped explicitly and only exists for OpenAI-style efforts.
        level_map: dict[str, str | None] = {}
        for level in PI_LEVELS:
            if level == "xhigh":
                supported = level in settings.reasoning_levels and protocol != "anthropic"
                level_map[level] = "xhigh" if supported else None
            elif level == "max" or level not in settings.reasoning_levels:
                level_map[level] = None
        entry["thinkingLevelMap"] = level_map
    return entry


def _codex_settings_args(provider: Provider) -> list[str]:
    """Codex config overrides for the launch model: context window and reasoning effort."""
    settings = provider.model_settings.get(provider.default_model)
    if settings is None:
        return []
    args: list[str] = []
    if settings.context_window:
        args += ["-c", f"model_context_window={settings.context_window}"]
    if settings.reasoning_default:
        effort = "none" if settings.reasoning_default == "off" else settings.reasoning_default
        args += ["-c", f"model_reasoning_effort={json.dumps(effort)}"]
    return args


def jcode_accepts_url(url: str) -> bool:
    """jcode 0.61 (jcode-provider-metadata normalize_api_base): plain http only for
    localhost, *.local, and loopback/private/link-local/CGNAT addresses."""
    parts = urlsplit(url)
    if parts.scheme == "https":
        return True
    host = (parts.hostname or "").lower()
    if host == "localhost" or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    # Mirror Rust's std::net predicates exactly; Python's is_private is wider.
    ranges: tuple[str, ...]
    if isinstance(ip, ipaddress.IPv4Address):
        ranges = (
            "127.0.0.0/8",
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "169.254.0.0/16",
            "0.0.0.0/32",
            "100.64.0.0/10",
        )
    else:
        ranges = ("::1/128", "fc00::/7", "fe80::/10", "::/128")
    return any(ip in ipaddress.ip_network(r) for r in ranges)


def _toml_string(value: str) -> str:
    """A JSON string literal is a valid TOML basic string (quotes/backslashes escaped)."""
    return json.dumps(value, ensure_ascii=False)


def render_jcode_sections(providers: dict[str, dict[str, object]]) -> str:
    """Render jcode 0.61 named provider profiles (`type = "openai-compatible"` only)."""
    lines: list[str] = []
    for name, entry in providers.items():
        lines += [
            f"[providers.{name}]",
            'type = "openai-compatible"',
            f"base_url = {_toml_string(str(entry['baseUrl']))}",
        ]
        if entry.get("authenticated"):
            lines += ['auth = "bearer"', f"api_key_env = {_toml_string(str(entry['apiKeyEnv']))}"]
        else:
            lines.append('auth = "none"')
        if entry.get("defaultModel"):
            lines.append(f"default_model = {_toml_string(str(entry['defaultModel']))}")
        models = entry["models"]
        assert isinstance(models, list)
        settings = entry.get("settings", {})
        assert isinstance(settings, dict)
        for model in models:
            lines += ["", f"[[providers.{name}.models]]", f"id = {_toml_string(str(model))}"]
            context = settings.get(model, {}).get("contextWindow") if settings else None
            if context:
                lines.append(f"context_window = {int(context)}")
        lines.append("")
    return "\n".join(lines)


#: SCHEMA GATE: reserved environment names per plan agent, from
#: docs/agents/index.md; confirm against the shipped images (spec Task 0 /
#: smoke Task 9) and extend here if the images read more routing variables.
PLAN_RESERVED_ENV = {
    "pi": {"PI_CODING_AGENT_DIR"},
    "codex": {
        "CODEX_HOME",
        "CODEX_OSS_BASE_URL",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
    },
    "opencode": {
        "OPENCODE_CONFIG",
        "OPENCODE_CONFIG_DIR",
        "OPENCODE_CONFIG_CONTENT",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
    },
    "tau": {"TAU_HOME", "TAU_CONFIG_DIR", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"},
    "jcode": {"JCODE_HOME", "JCODE_CONFIG_DIR", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"},
}


def prepare_native_provider(
    agent: str,
    names: list[str],
    configured_env: dict[str, str],
) -> tuple[dict[str, str], list[str], list[str]]:
    """Return launch env, routing arguments, and default-model arguments.

    Model arguments (selection, limits, reasoning level of the saved default)
    are dropped by the caller when the user passes an explicit model.
    """
    reserved = PLAN_RESERVED_ENV[agent]
    conflicts = sorted(
        k for k in configured_env if k in reserved or k.startswith("VIBEPOD_PROVIDER_")
    )
    if conflicts:
        raise ValueError(
            "Provider selection conflicts with environment settings: " + ", ".join(conflicts),
        )
    if not names or len(names) != len(set(names)):
        raise ValueError("Select distinct providers for this launch")
    if agent == "codex" and len(names) != 1:
        raise ValueError("Codex requires exactly one provider per launch")
    providers = [load_provider(name) for name in names]
    if agent == "codex":
        if providers[0].protocol != "openai-responses":
            raise ValueError(
                "Codex requires an OpenAI Responses-compatible provider, "
                "not Chat Completions or Anthropic",
            )
        if not providers[0].default_model:
            raise ValueError("Select a default model for this provider before launching Codex")
    if agent == "jcode" and any(p.protocol != "openai-chat" for p in providers):
        # jcode 0.61 named profiles are `type = "openai-compatible"` only.
        raise ValueError("Jcode named providers support OpenAI Chat Completions only")
    env: dict[str, str] = {}
    catalog: dict[str, object] = {}
    generic: dict[str, dict[str, object]] = {}
    routing_args: list[str] = []
    model_args: list[str] = []
    for index, provider in enumerate(providers):
        if not provider.models:
            raise ValueError(
                f"Provider '{provider.name}' has no selected models; use `vp provider edit`",
            )
        key = resolve_key(provider)
        url = provider.base_url
        if key and urlsplit(url).scheme != "https":
            raise ValueError("Authenticated provider launches require an HTTPS endpoint")
        if agent == "jcode" and not jcode_accepts_url(url):
            raise ValueError(
                "Jcode refuses plain-http provider URLs unless the host is localhost, "
                f"*.local, or a private IP address; '{provider.name}' uses {url}. "
                "Register the server's LAN IP instead of a hostname.",
            )
        if agent == "pi":
            key_env = f"VIBEPOD_PROVIDER_KEY_{index}"
            # References, never raw strings: Pi config strings can execute !commands.
            env[key_env] = key or "vibepod-local"
            catalog[provider.name] = {
                "baseUrl": url,
                "api": PI_APIS[provider.protocol],
                "apiKey": f"${key_env}",
                "models": [
                    _pi_model(model, provider.model_settings.get(model), provider.protocol)
                    for model in provider.models
                ],
            }
            if len(providers) == 1 and provider.default_model:
                routing_args = ["--provider", provider.name]
                model_args = ["--model", provider.default_model]
                default_settings = provider.model_settings.get(provider.default_model)
                if default_settings is not None and default_settings.reasoning_default:
                    model_args += ["--thinking", default_settings.reasoning_default]
            continue
        if agent == "codex":
            fields = {
                "name": provider.name,
                "base_url": url,
                "wire_api": "responses",
                "requires_openai_auth": False,
            }
            if key:
                key_env = f"VIBEPOD_PROVIDER_KEY_{index}"
                env[key_env] = key
                fields["env_key"] = key_env
            table = (
                "{"
                + ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in fields.items())
                + "}"
            )
            routing_args = [
                "-c",
                'model_provider="vibepod"',
                "-c",
                f"model_providers.vibepod={table}",
                "-c",
                'cli_auth_credentials_store="file"',
            ]
            model_args = [*_codex_settings_args(provider), "--model", provider.default_model]
            continue
        # OpenCode, Tau, Jcode: generic catalog. The container bootstrap renders
        # the native config and resolves the key from the named env variable.
        key_env = f"VIBEPOD_PROVIDER_KEY_{index}"
        # Always referenced: Tau's catalog requires api_key_env and OpenAI-style
        # clients require some key even for unauthenticated local servers.
        env[key_env] = key or "vibepod-local"
        entry: dict[str, object] = {
            "protocol": provider.protocol,
            "baseUrl": url,
            "models": list(provider.models),
            "apiKeyEnv": key_env,
            "authenticated": bool(key),
        }
        settings = settings_plan(provider)
        if settings:
            entry["settings"] = settings
        if len(providers) == 1 and provider.default_model:
            entry["defaultModel"] = provider.default_model
            # Native selection flags; nothing is passed without a saved default.
            if agent == "tau":
                routing_args = ["--provider", provider.name]
                model_args = ["--model", provider.default_model]
            elif agent == "jcode":
                routing_args = ["--provider-profile", provider.name]
                model_args = ["--model", provider.default_model]
        generic[provider.name] = entry
    env[PLAN_ENV] = json.dumps({"agent": agent, "providers": catalog if agent == "pi" else generic})
    if agent == "jcode":
        env[JCODE_CONFIG_ENV] = render_jcode_sections(generic)
        env[JCODE_NAMES_ENV] = " ".join(generic)
    return env, routing_args, model_args
