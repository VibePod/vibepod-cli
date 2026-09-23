"""Opt-in temporary provider routing: environment-routed agents and native-config dispatch.

Environment-routed agents (Claude, Qwen) receive base URL, key, and model purely
through their documented variables. Agents with native provider files go through
provider_adapters. No global store is mounted in containers.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from vibepod.core.agents import get_agent_spec
from vibepod.core.provider_adapters import prepare_native_provider
from vibepod.core.providers import load_provider, resolve_key

NATIVE_AGENTS = frozenset({"pi", "codex", "opencode", "tau", "jcode"})

#: Qwen's documented routing variables (docs/agents/index.md: "Qwen Code honors
#: the standard OPENAI_* / ANTHROPIC_* / GEMINI_API_KEY env vars"). QWEN_MODEL
#: is a documented alias and is deliberately not set.
QWEN_RESERVED_ENV = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "QWEN_MODEL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "GEMINI_API_KEY",
    },
)

QWEN_ENV_MAP: dict[str, dict[str, str | list[str]]] = {
    "openai-chat": {
        "base_url": "OPENAI_BASE_URL",
        "api_key": "OPENAI_API_KEY",
        "model": "OPENAI_MODEL",
    },
    "anthropic": {
        "base_url": "ANTHROPIC_BASE_URL",
        "api_key": "ANTHROPIC_API_KEY",
        "model": "ANTHROPIC_MODEL",
    },
}


@dataclass(frozen=True)
class ProviderLaunch:
    """Prepared launch: environment, routing arguments, default-model arguments."""

    env: dict[str, str]
    routing_args: list[str] = field(default_factory=list)
    model_args: list[str] = field(default_factory=list)

    def arguments(self, passthrough_args: list[str]) -> list[str]:
        """Routing plus the saved default's model arguments, unless the user
        passed an explicit model: then every argument tied to that default
        (selection, limits, reasoning level) is dropped."""
        explicit = any(
            arg in ("--model", "-m") or arg.startswith("--model=") for arg in passthrough_args
        )
        return [*self.routing_args] if explicit else [*self.routing_args, *self.model_args]


@dataclass(frozen=True)
class EnvRouting:
    """How one agent takes provider routing from environment variables."""

    label: str
    #: protocol -> {"base_url" | "api_key" | "model": target name(s)}
    protocols: dict[str, dict[str, str | list[str]]]
    reserved: frozenset[str]
    protocol_hint: str
    model_args: tuple[str, ...] = ()
    #: Variable receiving the launch model's max_output_tokens setting, if any.
    max_output_env: str = ""


def _targets(value: str | list[str]) -> list[str]:
    return [value] if isinstance(value, str) else list(value)


def _claude_routing() -> EnvRouting:
    spec = get_agent_spec("claude")
    assert spec.llm_env_map is not None
    reserved = {
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    }
    for target in spec.llm_env_map.values():
        reserved.update(_targets(target))
    return EnvRouting(
        "Claude",
        {"anthropic": dict(spec.llm_env_map)},
        frozenset(reserved),
        "an Anthropic-compatible provider",
        ("--model",),
        max_output_env="CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    )


def _qwen_routing() -> EnvRouting:
    return EnvRouting(
        "Qwen",
        QWEN_ENV_MAP,
        QWEN_RESERVED_ENV,
        "an OpenAI Chat Completions or Anthropic compatible provider",
    )


ENV_ROUTED: dict[str, Callable[[], EnvRouting]] = {
    "claude": _claude_routing,
    "qwen": _qwen_routing,
}


def _prepare_env_routed(
    routing: EnvRouting,
    names: list[str],
    configured_env: dict[str, str],
) -> ProviderLaunch:
    conflicts = sorted(
        k for k in configured_env if k in routing.reserved or k.startswith("VIBEPOD_PROVIDER_")
    )
    if conflicts:
        raise ValueError(
            "Provider selection conflicts with environment settings: " + ", ".join(conflicts),
        )
    if len(names) != 1:
        raise ValueError(f"{routing.label} requires exactly one provider per launch")
    provider = load_provider(names[0])
    if provider.protocol not in routing.protocols:
        raise ValueError(f"{routing.label} requires {routing.protocol_hint}")
    if not provider.default_model:
        raise ValueError(
            f"Select a default model for this provider before launching {routing.label}",
        )
    key = resolve_key(provider)
    url = provider.base_url
    if key and urlsplit(url).scheme != "https":
        raise ValueError("Authenticated provider launches require an HTTPS endpoint")
    values = {
        "base_url": url,
        # These clients require an API key even when a local server ignores it.
        "api_key": key or "vibepod-local",
        "model": provider.default_model,
    }
    env = {
        target: values[field]
        for field, targets in routing.protocols[provider.protocol].items()
        for target in _targets(targets)
    }
    settings = provider.model_settings.get(provider.default_model)
    if routing.max_output_env and settings is not None and settings.max_output_tokens:
        env[routing.max_output_env] = str(settings.max_output_tokens)
    model_args = [*routing.model_args, provider.default_model] if routing.model_args else []
    return ProviderLaunch(env, [], model_args)


def prepare_launch(agent: str, names: list[str], configured_env: dict[str, str]) -> ProviderLaunch:
    """Prepare private launch environment and arguments, without profile writes."""
    if agent in NATIVE_AGENTS:
        env, routing_args, model_args = prepare_native_provider(agent, names, configured_env)
        return ProviderLaunch(env, routing_args, model_args)
    routing = ENV_ROUTED.get(agent)
    if routing is None:
        raise ValueError(
            f"Temporary provider injection is not yet supported for '{agent}'. "
            "Its native profile configuration remains unchanged.",
        )
    return _prepare_env_routed(routing(), names, configured_env)


def prepare_provider(
    agent: str,
    names: list[str],
    configured_env: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    """Environment and full argument list (routing plus default-model arguments)."""
    launch = prepare_launch(agent, names, configured_env)
    return launch.env, [*launch.routing_args, *launch.model_args]
