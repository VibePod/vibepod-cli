# Adding an agent

A supported agent has two halves, and they live in two repositories:

| Repository | What it holds |
| --- | --- |
| [`vibepod-agents`](https://github.com/VibePod/vibepod-agents) | The container image, `docker/<agent>/`, published as `vibepod/<agent>` |
| [`vibepod-cli`](https://github.com/VibePod/vibepod-cli) | The registration: agent ID, shortcut, launch spec, defaults, docs and tests |

Each half is its own pull request. Both can be built and run together on your
machine before either one is published; see
[Test both halves locally](#test-both-halves-locally).

On this page `<agent>` stands for the agent ID (`qwen`) and `<AGENT>` for its
upper-case form (`QWEN`).

## Before you start

Run the upstream CLI once outside VibePod and answer these questions. The
answers decide most of what follows.

| Question | What it decides |
| --- | --- |
| How is it installed, and how is a version pinned? | The Dockerfile, and `automation.source` in `agents.json` |
| Where does it keep config, credentials and sessions? | The mount contract: `config_mount_path` and what the entrypoint persists |
| How does it log in, and does that work without a browser or a desktop keyring? | The entrypoint, and the authentication notes in the docs |
| Does it honor `HTTPS_PROXY` and a custom CA bundle? | Whether its traffic reaches the dashboard, and whether the entrypoint needs `setup_proxy_ca` |
| Does it have a one-shot, non-interactive mode? | `headless_prefix`, which enables `vp task` |
| Does it have an auto-approve flag? | `ikwid_args`, which `--ikwid` appends |
| Does it print a resume command when it exits? | A pattern in `src/vibepod/core/resume.py` |
| Does it ship an ACP adapter? | `acp_command`, which enables `--acp` |

Take the flags from the binary's own `--help`, not only from the vendor's
documentation: the two do not always agree.

## 1. The image (`vibepod-agents`)

Copy the closest existing image and adapt it: `docker/qwen/` for an npm
package, `docker/tau/` for a PyPI package, `docker/cursor/` for a vendor
binary.

| File | Change |
| --- | --- |
| `docker/<agent>/Dockerfile` | Install the CLI at a pinned version (`ARG <AGENT>_VERSION=...`). End with `WORKDIR /workspace`, the entrypoint and `CMD ["<agent>"]`. |
| `docker/<agent>/entrypoint.sh` | See the entrypoint contract below. |
| `compose.yml` | A service named `<agent>` with `./docker/<agent>` as build context. |
| `agents.json` | The catalog entry: `version_command`, `version_build_arg`, `platforms`, and the `automation.source` the release automation polls for new upstream versions. |
| `.github/workflows/build.yml` | `<agent>` in the build matrix. |
| `README.md` | The layout tree and the image targets table. |

### Entrypoint contract

Every image follows the same rules, so the CLI can treat them alike:

- VibePod passes the host user as `USER_UID` and `USER_GID`. Create a matching
  user, hand it the persisted directory, and start the agent with
  `exec gosu "$USER_UID:$USER_GID" "$@"`. Files the agent writes to the
  workspace then belong to the host user.
- When `USER_UID` is `0` (rootless Podman), skip the user setup and
  `exec "$@"` directly.
- Pass `"$@"` on unchanged, so that prompts with spaces and flags after `--`
  reach the agent intact.
- Keep the directory that holds credentials private (`chmod 700`).
- With the proxy enabled (the default), VibePod sets `HTTP_PROXY`,
  `HTTPS_PROXY`, `SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS`, `REQUESTS_CA_BUNDLE`
  and `CURL_CA_BUNDLE`. An agent that reads none of the CA variables needs the
  proxy CA appended to the system bundle; `setup_proxy_ca` in
  `docker/claude/entrypoint.sh` shows how.

### Checks

```bash
node --test
IMAGE_TAG=ci-smoke node scripts/smoke-agent-images.mjs --targets <agent> --build
pre-commit run --all-files
```

The smoke test builds the image, checks that arguments are forwarded through
the entrypoint, and runs the catalog's `version_command`.

## 2. The registration (`vibepod-cli`)

Mirror an existing agent end to end; `qwen` is a good template.

| File | Change |
| --- | --- |
| `src/vibepod/constants.py` | The ID in `SUPPORTED_AGENTS`, a shortcut in `AGENT_SHORTCUTS`, optional aliases in `AGENT_ALIASES`, `VP_IMAGE_<AGENT>` in `IMAGE_OVERRIDE_ENV_KEYS`, and the default image in `get_default_images()`. |
| `src/vibepod/core/agents.py` | The `AGENT_SPECS` entry. `config_mount_path` and `extra_env` must match what the image entrypoint expects. Set `ikwid_args`, `headless_prefix` and `acp_command` only for what the upstream CLI really supports. |
| `src/vibepod/core/config.py` | The per-agent defaults block. |
| `src/vibepod/commands/run.py` | The container path for user skills, in `_agent_skill_paths`. |
| `src/vibepod/core/resume.py` | The resume-hint pattern, if the agent prints one. |
| `src/vibepod/core/agent_import.py` | `IMPORT_SPECS` and `AGENT_ROOTS`: which host files `vp import` copies and where they land inside the mount. |
| `tests/` | The agent's cases in `test_agents.py`, `test_cli.py`, `test_constants.py`, `test_config.py`, `test_run.py`, `test_task_cmd.py`, `test_resume.py` and `test_agent_import.py`. |
| `docs/`, `README.md` | Every agent table and list, a section under [Individual agents](agents/index.md#individual-agents), and the tables in [Importing an existing setup](import.md). |

`grep -rn qwen README.md docs src tests` lists every place an agent appears,
which makes a complete checklist for the new one.

### Checks

```bash
python -m pytest
python -m ruff check . && python -m ruff format --check .
python -m mypy src
python scripts/check_import_docs.py
python -m mkdocs build --strict
```

## Test both halves locally

With both repositories checked out next to each other, build the image under a
`local` tag and run the CLI from a virtualenv against it:

```bash
cd vibepod-agents
docker build -t vibepod/<agent>:local docker/<agent>

cd ../vibepod-cli
python -m venv .venv && .venv/bin/pip install -e .
VP_IMAGE_<AGENT>=vibepod/<agent>:local .venv/bin/vp run <agent>
```

Nothing has to be published for this to work:

- `VP_IMAGE_<AGENT>` replaces the agent's default image for that command.
- VibePod only auto-pulls `latest` tags, so the `local` tag is taken straight
  from your machine.
- The virtualenv leaves the `vp` you already have installed untouched.

The same works for an agent that already exists, which is a quick way to try
an image change: `docker build -t vibepod/qwen:local docker/qwen`, then
`VP_IMAGE_QWEN=vibepod/qwen:local .venv/bin/vp run qwen`.

Then walk through what a user will do. Export the variable once
(`export VP_IMAGE_<AGENT>=vibepod/<agent>:local`) so every command below uses
the local image.

| Check | How |
| --- | --- |
| First run | `vp run <agent>`, log in, give it a small task, exit. |
| The login persists | Run it again; it must not ask for a login. State lives under `~/.config/vibepod/agents/<agent>/`. |
| File ownership | Let the agent create a file in the workspace; `ls -l` shows your own user. |
| Argument passthrough | `vp run <agent> -- <flag> "two words"` reaches the agent as written. |
| Auto-approve | `vp run <agent> --ikwid`, if the spec sets `ikwid_args`. |
| Task mode | `vp task create <agent> "Say hello"` and `vp task logs <id>`, if the spec sets `headless_prefix`. |
| Traffic logging | `vp logs start`; the session's requests appear in the dashboard. |
| Resume hint | After exit, VibePod prints the matching `vp run <agent> -- ...` command, if you added a pattern. |
| Import | `vp import <agent> --dry-run` lists only files that belong to this agent. |

If you have rootless Podman at hand, repeat the first three checks there: it
takes the `USER_UID=0` path of the entrypoint.

## Open the pull requests

Use the same branch name in both repositories, for example `<agent>-agent`,
and say in each description how you tested: the commands above are enough.

1. **`vibepod-agents` first.** Once it is merged, the `auto-release` workflow
   builds the new target and publishes `vibepod/<agent>:latest` on its next
   run. It runs every six hours and can be dispatched by hand.
2. **`vibepod-cli` second.** Its `default-images` CI job checks that every
   default image exists on Docker Hub (`scripts/check_default_images.py`), so
   that job fails until step 1 has published the image.
3. **The website last.** The homepage in
   [`vibepod.github.io`](https://github.com/VibePod/vibepod.github.io) names
   the supported agents and shows one screenshot each; add the new agent
   there once the CLI change is released.
