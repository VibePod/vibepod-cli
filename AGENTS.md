# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.

## Adding a supported agent

To add a new agent, mirror the `qwen` (or `freebuff`) entry end-to-end:
`SUPPORTED_AGENTS`/`AGENT_SHORTCUTS`/`AGENT_ALIASES`/`IMAGE_OVERRIDE_ENV_KEYS`
and `get_default_images()` in `src/vibepod/constants.py`, the `AGENT_SPECS`
entry in `src/vibepod/core/agents.py` (mount contract must match the
vibepod-agents image entrypoint), the per-agent defaults in
`src/vibepod/core/config.py`, skills paths in
`src/vibepod/commands/run.py::_agent_skill_paths`, and the docs + tests
(`docs/agents/index.md`, `README.md`, `docs/quickstart.md`, `docs/index.md`,
`docs/configuration.md`, `tests/test_{agents,cli,constants,run,task_cmd}.py`).
The local-image fallback (`VP_IMAGE_<AGENT>` override + pull-failure fallback
in `run.py`/`task.py`) is how an unreleased image is exercised locally.

## State-reporting integrations (herdr, dash)

`core/herdr.py` and `core/dash.py` share the same shape: a config gate, a
data-driven map of vendored files to inject into the agent config dir, and
per-agent registration (claude `settings.json`, codex `.codex/hooks.json`).
Both copy through `core/hooksync.py`, and both must soft-fail — a broken
integration never blocks a run. The dash client scripts are vendored from the
vibepod-dash repo; see `src/vibepod/resources/dash/README.md` before editing
them. Codex registration is shared through `core/codex_hooks.py`, which lets
Dash and herdr coexist and removes only VibePod's legacy `notify` entries.

## Tests

Runner is `pytest` (`python -m pytest`); CI also validates default images with
`scripts/check_default_images.py`. The suite is hermetic only when the host
`~/.config/vibepod/config.yaml` is not read: run with a throwaway
`VP_CONFIG_DIR=/tmp/...` when the host has a global config (its leaked
`agents.*.env` breaks `test_config.py`). `ruff check` + `ruff format --check`
and `mypy` are pre-commit gated.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
