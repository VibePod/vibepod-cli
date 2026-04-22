# Agents

VibePod manages each agent as a Docker container. Credentials and config are persisted to `~/.config/vibepod/agents/<agent>/` on your host and mounted into the container on every run, so you only need to authenticate once.

## Supported Agents

| Agent | Provider | Shortcut | Image |
|-------|----------|----------|-------|
| `claude` | Anthropic | `vp c` | `vibepod/claude:latest` |
| `gemini` | Google | `vp g` | `vibepod/gemini:latest` |
| `opencode` | OpenAI | `vp o` | `vibepod/opencode:latest` |
| `devstral` (alias: `vibe`) | Mistral | `vp d` | `vibepod/devstral:latest` |
| `auggie` | Augment Code | `vp a` | `vibepod/auggie:latest` |
| `copilot` | GitHub | `vp p` | `vibepod/copilot:latest` |
| `codex` | OpenAI | `vp x` | `vibepod/codex:latest` |

Alias note: `vp run vibe` resolves to `vp run devstral`.

## First run & authentication

Start any agent for the first time with `vp run <agent>`. The container will prompt you to authenticate (browser OAuth, API key entry, or device flow depending on the provider). Once authenticated, credentials are written to the persisted config directory and reused on subsequent runs.

## Auto-pulling the latest image

VibePod automatically pulls the latest image for an agent before every run. This ensures you always start with the most up-to-date container without manual intervention.

To disable auto-pull globally:

```yaml
auto_pull: false
```

Or for a specific agent only:

```yaml
agents:
  devstral:
    auto_pull: false
```

Per-agent `auto_pull` takes precedence over the global setting. For example, you can disable it globally but keep it on for a specific agent:

```yaml
auto_pull: false          # skip pull by default
agents:
  claude:
    auto_pull: true       # except claude — always pull
```

You can also force a one-off pull via the CLI flag regardless of config:

```bash
vp run claude --pull
```

The resolution order is: `--pull` flag > per-agent `auto_pull` > global `auto_pull`.

## Overriding the image

You can point VibePod at a custom image via an environment variable:

```bash
VP_IMAGE_CLAUDE=myorg/my-claude:dev vp run claude
```

Or permanently via your [global config](../configuration.md):

```yaml
agents:
  claude:
    image: myorg/my-claude:dev
```

## Image customization workflows

VibePod has a fixed set of supported agent IDs (`claude`, `gemini`, `opencode`, `devstral`, `auggie`, `copilot`, `codex`). The CLI also supports the alias `vibe`, which resolves to `devstral`. Image customization means changing the image used for one of those IDs.

### 1. Extend an existing image for an agent

Example: add tools to the default Claude image.

1. Create a Dockerfile that extends the current base image.

```dockerfile
# Dockerfile.claude
FROM vibepod/claude:latest

# Add project-specific utilities.
RUN apt-get update \
  && apt-get install -y --no-install-recommends ripgrep jq \
  && rm -rf /var/lib/apt/lists/*
```

2. Build and tag the derived image.

```bash
docker build -f Dockerfile.claude -t myorg/claude-container:with-tools .
```

3. Run with the new image (one-off) or set it in config (persistent).

```bash
# one-off
VP_IMAGE_CLAUDE=myorg/claude-container:with-tools vp run claude
```

```yaml
# ~/.config/vibepod/config.yaml (or .vibepod/config.yaml)
agents:
  claude:
    image: myorg/claude-container:with-tools
```

### 2. Add a new image for an agent

Example: point `opencode` at a newly published internal image.

1. Build/publish your image to a registry (for example `registry.example.com/team/opencode:2026-03-01`).
2. Attach that image to the target agent in config.

```yaml
agents:
  opencode:
    image: registry.example.com/team/opencode:2026-03-01
```

3. Start the agent.

```bash
vp run opencode
```

You can also test quickly without editing config:

```bash
VP_IMAGE_OPENCODE=registry.example.com/team/opencode:2026-03-01 vp run opencode
```

## Passing environment variables

Use `-e` / `--env` to inject variables at runtime:

```bash
vp run claude -e MY_VAR=value -e ANOTHER=123
```

Persistent per-agent env vars can also be set in config:

```yaml
agents:
  claude:
    env:
      MY_VAR: value
```

## Passing arguments to the agent

Any extra arguments after the agent name are appended to the agent command inside the container:

```bash
vp run <agent> <agent-args>
```

Use `--` before agent flags so VibePod does not parse them as its own options:

```bash
vp run <agent> -- <agent-flag> <value>
```

For concrete syntax, check the agent's own CLI help. For example, Claude and
Codex both accept model flags, but their exact flag names and values differ.

## Init scripts before startup

Use `agents.<agent>.init` to run shell commands in the container before the agent launches. This is useful for installing extra tools in a custom image workflow.

```yaml
agents:
  codex:
    init:
      - apt-get update
      - apt-get install -y ripgrep jq
```

The `init` commands run on every `vp run` for that agent and must be idempotent.

## IKWID mode (`--ikwid`)

Use `--ikwid` to enable each agent's built-in auto-approval / permission-skip mode when supported.

| Agent | `--ikwid` appended args |
|-------|--------------------------|
| `claude` | `--dangerously-skip-permissions` |
| `gemini` | `--approval-mode=yolo` |
| `devstral` | `--auto-approve` |
| `copilot` | `--yolo` |
| `codex` | `--dangerously-bypass-approvals-and-sandbox` |
| `opencode` | Not supported |
| `auggie` | Not supported |

Example:

```bash
vp run codex --ikwid
```

## Detached mode

Use `-d` / `--detach` to start an agent container in the background without attaching your terminal. The agent process starts immediately inside the container — `-d` only controls whether VibePod attaches your terminal to it.

### Basic usage

```bash
vp run claude -d
# ✓ Started vibepod-claude-a1b2c3d4
```

The command prints the container name and returns immediately. You can also find it later with:

```bash
vp list --running
```

### Interacting with a detached container

The agent is already running inside the container. You can exec into it to inspect state, install extra tools, or interact with the agent alongside its running process:

```bash
docker exec -it vibepod-claude-a1b2c3d4 bash
```

Use the container name printed by `vp run -d` or shown in `vp list`.

!!! tip
    If you need to run setup commands **before** the agent launches, use [`agents.<agent>.init`](#init-scripts-before-startup) or [extend the base image](#image-customization-workflows) instead — these run inside the container before the agent process starts.

### Managing detached containers

Check running agents:

```bash
vp list              # shows running + configured agents
vp list --running    # shows only running agents
vp list --json       # machine-readable output
```

Stop a specific agent, a single container, or all agents:

```bash
vp stop claude                       # stop every container for the `claude` agent
vp stop vibepod-claude-a1b2c3d4      # stop one specific container (from `vp list`)
vp stop claude -f                    # force stop immediately
vp stop --all                        # stop every VibePod container
```

The argument is resolved as an agent name/shortcut first; anything else is looked up as a container name or ID. Only VibePod-managed containers can be stopped this way.

### Caveats

- **`auto_remove` (default: `true`)** — By default, containers are automatically removed when they stop. This means you cannot restart a stopped detached container; you need to `vp run` again. Set `auto_remove: false` in your [configuration](../configuration.md) if you want stopped containers to persist.
- **Session logging** — Sessions started with `--detach` are not recorded in the VibePod session log since VibePod does not capture the interactive I/O. If you need session logging, run without `--detach`.

## Reattaching a terminal

Closing the terminal window that runs `vp run` does **not** stop the container — the agent keeps running in the background under Docker. This is by design: the container's lifecycle is tied to Docker, not to your shell. Use it as a feature when you want to keep a long-running session alive across terminal restarts.

To rejoin a running container:

```bash
vp list --running       # find the container name
vp attach <container>   # reattach your terminal
```

If exactly one managed container is running you can omit the name:

```bash
vp attach
```

`vp attach` only works for containers that are already running and managed by VibePod. When you are done, close the terminal to leave it running, or stop it explicitly with `vp stop <container>`, `vp stop <agent>`, or `vp stop --all`.

## Connecting to a Docker Compose network

When your workspace contains a `docker-compose.yml` or `compose.yml`, VibePod detects it and offers to connect the agent container to an existing network so it can reach your running services.

You can also specify the network explicitly:

```bash
vp run claude --network my-compose-network
```

## Individual agents

### Claude (Anthropic)

```bash
vp run claude   # or: vp c
```

Credentials are stored in `~/.config/vibepod/agents/claude/`. On first run, Claude's interactive setup will guide you through API key configuration.

#### Long-lived token (recommended)

Claude Code has a known upstream bug where OAuth access tokens (~8 h TTL) are not automatically refreshed from disk, forcing users to run `/login` roughly once per day. See [Why this workaround exists](#why-this-workaround-exists) below for the full bug history and links.

VibePod works around this by storing a ~1-year long-lived token on the host and injecting it as `CLAUDE_CODE_OAUTH_TOKEN` on every run. This sidesteps the refresh path entirely.

!!! info "This is an official authentication method"
    `claude setup-token` and the `CLAUDE_CODE_OAUTH_TOKEN` environment variable are both documented by Anthropic as a supported authentication path for CI pipelines, scripts, and other environments where an interactive browser login isn't available. See the [official Claude Code authentication docs](https://code.claude.com/docs/en/authentication#long-lived-tokens) and the [`claude-code-action` setup guide](https://github.com/anthropics/claude-code-action/blob/main/docs/setup.md). VibePod just automates the storage and injection.

**One-time setup:**

```bash
vp run claude setup-token
```

This starts the container with `claude setup-token`, which opens Anthropic's OAuth flow in your browser. After you authorise, the container prints a token. VibePod then prompts you to paste it and saves it to:

```text
~/.config/vibepod/agents/claude/oauth-token   (mode 0600)
```

**Subsequent runs:**

```bash
vp run claude
```

VibePod detects the stored token and injects `CLAUDE_CODE_OAUTH_TOKEN` automatically. Look for `Using stored Claude OAuth token` in the startup output to confirm.

**Precedence** (first match wins):

1. `-e ANTHROPIC_API_KEY=...` or `-e CLAUDE_CODE_OAUTH_TOKEN=...` passed on the CLI
2. `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` set in your per-agent `env:` config
3. Stored `oauth-token` file
4. Interactive OAuth via `.credentials.json` (subject to the refresh bug)

**Verifying the token is stored:**

```bash
vp doctor claude
```

Shows credentials state, stored-token presence and mtime, and which auth mode the next run will use. You can also inspect the file directly:

```bash
ls -l ~/.config/vibepod/agents/claude/oauth-token
# or to view contents (treat as a secret — do not share):
nano ~/.config/vibepod/agents/claude/oauth-token
```

**Verifying the token works:**

```bash
vp run claude -p "say ok"
```

`-p` runs Claude Code in headless mode — one API call, one response. If you see "ok", the token is valid.

**Caveats:**

- The long-lived token is **inference-only** — it cannot establish [Remote Control](https://code.claude.com/docs/en/remote-control) sessions (steering a container from claude.ai/code or the mobile app).
- `claude setup-token` requires a **Pro, Max, Team, or Enterprise** plan. Console (pay-per-token) accounts should use `ANTHROPIC_API_KEY` instead.
- The token rotates roughly once a year. When it expires, just run `vp run claude setup-token` again.

#### Using an API key instead

If you're on a Console (pay-per-token) account, set `ANTHROPIC_API_KEY` and skip the setup-token flow entirely:

```bash
vp run claude -e ANTHROPIC_API_KEY=sk-ant-...
```

Or permanently in config:

```yaml
agents:
  claude:
    env:
      ANTHROPIC_API_KEY: sk-ant-...
```

#### Diagnostics

`vp doctor claude` is the first tool to reach for when auth misbehaves. It reports:

- `.credentials.json` — file owner/mode, `expiresAt`, presence of `refreshToken`, scopes, subscription type
- `.claude.json` — mtime cross-check
- Stored long-lived token state
- Which host env vars (`ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CONFIG_DIR`) are set
- **Effective auth mode** — what the next `vp run claude` will actually use

Exit codes: `0` healthy, `1` config dir missing, `2` OAuth token expired (useful in scripts).

#### Why this workaround exists

The root cause is in Claude Code itself, not in VibePod. The OAuth `refreshToken` is stored in `.credentials.json` but never used: the access token is loaded from disk, sent as-is until it 401s, and nothing is written back when a refresh would have succeeded. The bug affects native Linux, WSL, macOS, and every container-based deployment equally.

Community forensics ([#33995 comment](https://github.com/anthropics/claude-code/issues/33995#issuecomment-2718892341)):

> Set `expiresAt` in `~/.claude/.credentials.json` to `Date.now()` to force expiry. Send a message — Claude processes it successfully, meaning the in-memory token refresh worked. Check `~/.claude/.credentials.json` afterward — file was never written. Conclusion: `refreshOAuthToken` succeeds and returns new tokens, but the credential store's `update()` is never called (or silently fails) after a successful refresh. The new token lives only in memory. Next session launch reads the stale expired token from disk and requires re-login.

The community-validated workaround ([#24317 comment](https://github.com/anthropics/claude-code/issues/24317#issuecomment-2664923815)) is exactly what VibePod implements:

> I worked around this using `claude setup-token` and then feeding it in as the `CLAUDE_CODE_OAUTH_TOKEN` environment variable. It skips all the "OAuth tokens invalidating each other", but has the downside that it doesn't allow `/usage`.

`claude setup-token` itself is an **officially supported** Claude Code authentication path, documented for exactly this kind of non-interactive deployment. See Anthropic's [authentication guide](https://code.claude.com/docs/en/authentication#long-lived-tokens) and the [`claude-code-action` setup guide](https://github.com/anthropics/claude-code-action/blob/main/docs/setup.md) — the same mechanism used by Anthropic's own GitHub Action.

**Upstream tracking issues — core bug (access-token not refreshed from disk):**

| # | Status | Summary |
|---|---|---|
| [#50743](https://github.com/anthropics/claude-code/issues/50743) | open · `has repro` · `area:auth` | Newest and cleanest repro on headless Linux — `refreshToken` ignored |
| [#42904](https://github.com/anthropics/claude-code/issues/42904) | closed as duplicate | Canonical "daily re-login required for subscription users" report |
| [#40985](https://github.com/anthropics/claude-code/issues/40985) | open · `stale` | "Auth tokens expire too frequently" — confirms ~8 h TTL |
| [#33995](https://github.com/anthropics/claude-code/issues/33995) | closed not-planned | Best technical forensics (quoted above); proves write-back is the broken step |
| [#21765](https://github.com/anthropics/claude-code/issues/21765) | closed not-planned | First clear statement: "Claude Code doesn't use refresh tokens to get new access tokens" |
| [#12447](https://github.com/anthropics/claude-code/issues/12447) | open | OAuth expiry disrupts autonomous workflows; refresh token handling needed |
| [#37402](https://github.com/anthropics/claude-code/issues/37402) | open | `--print` / automation mode also affected |

**Multi-session race condition** (why a shared `.credentials.json` across simultaneous sessions makes things worse):

| # | Status | Summary |
|---|---|---|
| [#24317](https://github.com/anthropics/claude-code/issues/24317) | open · `has repro` · 18 comments | Canonical thread; documents refresh-token rotation and single-use semantics |
| [#48786](https://github.com/anthropics/claude-code/issues/48786) | closed as dup of #24317 | Independent reproduction |
| [#27933](https://github.com/anthropics/claude-code/issues/27933) | closed | Early race-condition report |
| [#45129](https://github.com/anthropics/claude-code/issues/45129) | closed as dup | Agent worktree subprocesses hit this constantly |

**Container / headless specifically:**

| # | Status | Summary |
|---|---|---|
| [#22066](https://github.com/anthropics/claude-code/issues/22066) | closed as duplicate | OAuth authentication not persisting in Docker |
| [#34917](https://github.com/anthropics/claude-code/issues/34917) | closed | OAuth "Redirect URI not supported" in headless/Docker |
| [#34141](https://github.com/anthropics/claude-code/issues/34141) | closed | Claude Code ignores `ANTHROPIC_API_KEY` when OAuth redirect fails in devcontainers |
| [#7100](https://github.com/anthropics/claude-code/issues/7100) | closed not-planned | Request for official headless-auth documentation |
| [#22992](https://github.com/anthropics/claude-code/issues/22992) | open | Feature request: RFC 8628 device-code flow for headless |

**Proxy / Cloudflare interaction** (relevant if you run vibepod behind the built-in mitmproxy):

| # | Status | Summary |
|---|---|---|
| [#47754](https://github.com/anthropics/claude-code/issues/47754) | open · `area:auth` · `platform:linux` | Cloudflare WAF blocks OAuth token refresh from headless Linux servers |
| [#33269](https://github.com/anthropics/claude-code/issues/33269) | open | Cloudflare challenge race during `auth login` / `setup-token` |

**Anthropic's posture:** most reports are auto-closed as duplicates by a bot; the core issues (#21765, #33995) were closed as "not planned." A changelog line for Claude Code v2.1.44 mentioned "Fixed auth refresh errors" but users report the same behaviour on every later version (v2.1.62, 2.1.74, 2.1.116 observed). No committed fix has landed as of this writing.

### Gemini (Google)

```bash
vp run gemini   # or: vp g
```

### OpenCode (OpenAI)

```bash
vp run opencode   # or: vp o
```

### Devstral / Vibe (Mistral)

```bash
vp run devstral   # or: vp d
vp run vibe       # alias of devstral
```

!!! note
    Devstral runs under your host user (uid:gid) and requires the `linux/amd64` platform. On Apple Silicon, Docker's Rosetta emulation is used automatically.

### Auggie (Augment Code)

```bash
vp run auggie   # or: vp a
```

### Copilot (GitHub)

```bash
vp run copilot   # or: vp p
```

### Codex (OpenAI)

```bash
vp run codex   # or: vp x
```
