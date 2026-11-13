# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.

## Adding a supported agent

To add a new agent, mirror the `qwen` (or `freebuff`) entry end-to-end:
`SUPPORTED_AGENTS`/`AGENT_SHORTCUTS`/`AGENT_ALIASES`/`IMAGE_OVERRIDE_ENV_KEYS`
and `get_default_images()` in `src/vibepod/constants.py`, the `AGENT_SPECS`
entry in `src/vibepod/core/agents.py` (mount contract must match the
vibepod-agents image entrypoint; set `acp_command` when the agent ships an
ACP adapter, see `docs/acp.md`), the per-agent defaults in
`src/vibepod/core/config.py`, skills paths in
`src/vibepod/commands/run.py::_agent_skill_paths`, the `IMPORT_SPECS` entry in
`src/vibepod/core/agent_import.py` (host config paths per category; destinations
must match the same mount contract, and `scripts/check_import_docs.py` gates
`docs/import.md`), and the docs + tests
(`docs/agents/index.md`, `README.md`, `docs/quickstart.md`, `docs/index.md`,
`docs/configuration.md`, `docs/import.md`,
`tests/test_{agents,cli,constants,run,task_cmd,agent_import}.py`).
The local-image fallback (`VP_IMAGE_<AGENT>` override + pull-failure fallback
in `run.py`/`task.py`) is how an unreleased image is exercised locally.
`docs/adding-an-agent.md` is the contributor walkthrough: the image half in
vibepod-agents, the local build-and-run test loop, and the order of the pull
requests.

Hermes is the exception to the `~/.agents/skills` convention: it reads that
directory only via the `skills.external_dirs` key in its own `config.yaml` and
has no env-var override, so the `vibepod-agents` image entrypoint seeds the key
rather than the CLI mounting a symlink.

Hermes also has two launch constraints: on rootless Podman it cannot use plain
keep-id (the image boots as root, then drops to its fixed `hermes` UID 10000 and
exits for any other non-root start), so `AgentSpec.rootless_runtime_ids` makes
`rootless_podman_identity` launch it with `keep-id:uid=10000,gid=10000` +
`--user 0:0`; and `/opt/hermes` (its install root) is reserved so an `--acp`
workspace bind cannot shadow the entrypoint/venv/`hermes-acp`.

Junie's image contract is `HOME=/config` plus `JUNIE_HOME=/config/.junie`, with
the app bundle under `/opt/junie` (see `_ACP_RESERVED_AGENT_PATHS`). Junie reads
user skills only from `$JUNIE_HOME/skills`; `_agent_skill_paths` resolves that
path.

Mistral Vibe uses the canonical ID `vibe` (`devstral` is a CLI/config alias).
Its published `devstral` image and credentials subdirectory are retained for
compatibility; `VP_IMAGE_VIBE` takes precedence over `VP_IMAGE_DEVSTRAL`.

Cline's image contract is `HOME=/config` with all state under `~/.cline`; its
install root is under `/usr`, already reserved for ACP binds. Tasks pass
`--act` because one-shot runs otherwise reuse the TUI's persisted plan/act
mode; `--ikwid` adds `--auto-approve true` (not `--yolo`, which switches to a
restricted single-turn mode).

Cursor's image contract is `HOME=/config` with `cursor-agent` under
`/opt/cursor-agent`, which is reserved for Cursor's ACP workspace binds (see
`_ACP_RESERVED_AGENT_PATHS`).
Detached Cursor tasks always pass `--trust` to avoid an interactive workspace
prompt; only `--ikwid` adds `--force` to bypass command approvals.

Warp's image contract is `HOME=/config`. Warp keeps its login only in the
Secret Service, so the image entrypoint starts a private D-Bus session bus and
an unlocked GNOME keyring whose files live in `/config/.local/share/keyrings`.
The CLI is interactive only (no one-shot mode, no ACP), so the spec has neither
`headless_prefix` nor `acp_command`. `vp import warp` must never list
`~/.local/share/keyrings`: on the host that is the user's whole login keyring.

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
