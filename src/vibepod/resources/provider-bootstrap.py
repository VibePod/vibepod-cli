#!/usr/bin/env python3
"""Container-local temporary provider configuration for the Tau image.

Mirrors provider-bootstrap.cjs: the real agent argv arrives as JSON in
VIBEPOD_PROVIDER_COMMAND; this script is launched as `python3 <mounted path>`
only, so image entrypoints that re-parse argv cannot mangle prompts.

Tau (tau-ai 0.3.x) has no config-root override, so HOME is redirected to a
private view in which every entry of the real home is symlinked and ~/.tau is
mirrored with a private catalog.toml (schema_version 1, [[providers]] entries).
Sessions stay persistent; native provider files are never written back. Error
output never includes parsed content: it can contain credential values. (Jcode
uses provider-bootstrap.sh: its image has no Python.)
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from types import FrameType
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - agent images ship Python 3.12+
    import tomli as tomllib

FAILURE = "Cannot prepare temporary provider configuration; check native profile files."

# Tau catalog kinds/APIs (tau_coding.provider_catalog.ProviderKind / ProviderApi).
TAU_KIND = {
    "openai-chat": "openai-compatible",
    "openai-responses": "openai-compatible",
    "anthropic": "anthropic",
}
TAU_API = {
    "openai-chat": "openai-completions",
    "openai-responses": "openai-responses",
    "anthropic": "anthropic-messages",
}
TAU_PRIVATE_FILES = {"catalog.toml", "providers.json", "credentials.json"}
# tau_coding.paths.TauPaths: created in the real profile before the view is built so
# the view links to them; otherwise Tau would create them inside the temporary view.
TAU_PERSISTENT_DIRS = ("sessions", "logs", "skills", "prompts", "themes")
# tau_coding.thinking.ThinkingParameter per wire protocol.
TAU_THINKING_PARAMETER = {
    "openai-chat": "reasoning_effort",
    "openai-responses": "reasoning.effort",
    "anthropic": "anthropic.thinking",
}
REASONING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh")


class NameClash(ValueError):
    """A VibePod provider name already exists in the native configuration."""


def toml_string(value: str) -> str:
    """A JSON string literal is a valid TOML basic string (quotes/backslashes escaped)."""
    return json.dumps(value, ensure_ascii=False)


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def symlink_entries(source: Path, target: Path, *, skip: set[str]) -> None:
    """Symlink every entry of ``source`` into ``target`` except ``skip``."""
    for name in sorted(set(os.listdir(source)) - skip):
        os.symlink(source / name, target / name)


def write_private(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)


def render_tau_catalog(original: str, providers: dict[str, Any]) -> str:
    """Append VibePod providers as ``[[providers]]`` entries to Tau's user catalog."""
    raw = tomllib.loads(original) if original.strip() else {}
    existing = raw.get("providers", [])
    if not isinstance(existing, list) or not all(isinstance(p, dict) for p in existing):
        raise ValueError("catalog providers must be an array of tables")
    clash = sorted({str(p.get("name")) for p in existing} & set(providers))
    if clash:
        raise NameClash(", ".join(clash))
    lines = [original.rstrip("\n")] if original.strip() else []
    if "schema_version" not in raw:
        lines.insert(0, "schema_version = 1")
    for name, entry in providers.items():
        models = list(entry["models"])
        lines += [
            "",
            "[[providers]]",
            f"name = {toml_string(name)}",
            f"display_name = {toml_string(name)}",
            f"kind = {toml_string(TAU_KIND[entry['protocol']])}",
            f"api = {toml_string(TAU_API[entry['protocol']])}",
            f"base_url = {toml_string(entry['baseUrl'])}",
            f"api_key_env = {toml_string(entry['apiKeyEnv'])}",
            f"models = [{', '.join(toml_string(m) for m in models)}]",
            # Tau's schema requires a default; the launch only selects it when
            # a VibePod default is saved (see --provider/--model arguments).
            f"default_model = {toml_string(entry.get('defaultModel') or models[0])}",
            f"docs_url = {toml_string(entry['baseUrl'])}",
        ]
        settings = entry.get("settings") or {}
        default_model = entry.get("defaultModel") or models[0]
        if any(settings.get(m, {}).get("reasoning") for m in models):
            lines.append(
                f"thinking_parameter = {toml_string(TAU_THINKING_PARAMETER[entry['protocol']])}",
            )
            default_level = settings.get(default_model, {}).get("reasoningDefault")
            if default_level:
                lines.append(f"thinking_default = {toml_string(default_level)}")
        # Sub-tables must follow every scalar key of this [[providers]] element.
        for model in models:
            model_settings = settings.get(model)
            if not model_settings:
                continue
            lines += ["", f"[providers.model_metadata.{toml_string(model)}]"]
            if model_settings.get("contextWindow"):
                lines.append(f"context_window = {int(model_settings['contextWindow'])}")
            if model_settings.get("maxOutputTokens"):
                lines.append(f"max_tokens = {int(model_settings['maxOutputTokens'])}")
            if "reasoning" in model_settings:
                lines.append(f"reasoning = {'true' if model_settings['reasoning'] else 'false'}")
            levels = model_settings.get("reasoningLevels")
            if levels:
                unsupported = [level for level in REASONING_LEVELS if level not in levels]
                rendered = ", ".join(toml_string(level) for level in unsupported)
                lines.append(f"unsupported_thinking_levels = [{rendered}]")
    return "\n".join(lines) + "\n"


def prepare_tau(plan: dict[str, Any], home: Path, view: Path) -> dict[str, str]:
    root = home / ".tau"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in TAU_PERSISTENT_DIRS:
        (root / name).mkdir(exist_ok=True, mode=0o700)
    (home / ".agents").mkdir(exist_ok=True, mode=0o700)
    symlink_entries(home, view, skip={".tau"})
    view_root = view / ".tau"
    view_root.mkdir(mode=0o700)
    symlink_entries(root, view_root, skip=TAU_PRIVATE_FILES)
    for name in ("providers.json", "credentials.json"):
        if (root / name).exists():
            shutil.copyfile(root / name, view_root / name)
            os.chmod(view_root / name, 0o600)
    catalog = render_tau_catalog(read_text(root / "catalog.toml"), plan["providers"])
    write_private(view_root / "catalog.toml", catalog)
    return {"HOME": str(view)}


PREPARERS = {"tau": prepare_tau}


def main() -> int:
    view: Path | None = None
    child: subprocess.Popen[bytes] | None = None

    def cleanup() -> None:
        if view is not None and view.exists():
            shutil.rmtree(view, ignore_errors=True)

    def forward(number: int, _frame: FrameType | None) -> None:
        if child is not None:
            child.send_signal(number)

    try:
        plan = json.loads(os.environ["VIBEPOD_PROVIDER_PLAN"])
        command = json.loads(os.environ.get("VIBEPOD_PROVIDER_COMMAND") or "null")
        prepare = PREPARERS.get(plan.get("agent"))
        valid_command = (
            isinstance(command, list)
            and bool(command)
            and all(isinstance(part, str) for part in command)
        )
        home_value = os.environ.get("HOME")
        if prepare is None or not valid_command or not home_value:
            return fail("Cannot prepare temporary provider configuration.")
        view = Path(tempfile.mkdtemp(prefix="vibepod-provider-"))
        os.chmod(view, 0o700)
        try:
            overrides = prepare(plan, Path(home_value), view)
        except NameClash as clash:
            cleanup()
            return fail(
                f"Provider name already defined in the native {plan['agent']} "
                f"configuration: {clash}. Rename the VibePod provider.",
            )
        env = {
            key: value
            for key, value in os.environ.items()
            # Key variables stay: the rendered catalog references them by name.
            if key not in {"VIBEPOD_PROVIDER_PLAN", "VIBEPOD_PROVIDER_COMMAND"}
        }
        env.update(overrides)
        try:
            child = subprocess.Popen(command, env=env)
        except OSError:
            cleanup()
            return fail("Could not start agent with temporary provider configuration.")
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, forward)
        status = child.wait()
        # Shell convention for a signal-terminated child, mirroring the Node bootstrap.
        return 128 - status if status < 0 else status
    except Exception:
        cleanup()
        return fail(FAILURE)
    finally:
        if child is not None and child.poll() is None:
            child.kill()
        cleanup()


if __name__ == "__main__":
    sys.exit(main())
