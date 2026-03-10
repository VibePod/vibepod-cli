# Agents

VibePod manages each agent as a Docker container. Credentials and config are persisted to `~/.config/vibepod/agents/<agent>/` on your host and mounted into the container on every run, so you only need to authenticate once.

## Supported Agents

| Agent | Provider | Shortcut | Image |
|-------|----------|----------|-------|
| `claude` | Anthropic | `vp c` | `nezhar/claude-container:latest` |
| `gemini` | Google | `vp g` | `nezhar/gemini-container:latest` |
| `opencode` | OpenAI | `vp o` | `nezhar/opencode-cli:latest` |
| `devstral` | Mistral | `vp d` | `nezhar/devstral-cli:latest` |
| `auggie` | Augment Code | `vp a` | `nezhar/auggie-cli:latest` |
| `copilot` | GitHub | `vp p` | `nezhar/copilot-cli:latest` |
| `codex` | OpenAI | `vp x` | `nezhar/codex-cli:latest` |

## First run & authentication

Start any agent for the first time with `vp run <agent>`. The container will prompt you to authenticate (browser OAuth, API key entry, or device flow depending on the provider). Once authenticated, credentials are written to the persisted config directory and reused on subsequent runs.

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

VibePod has a fixed set of supported agent IDs (`claude`, `gemini`, `opencode`, `devstral`, `auggie`, `copilot`, `codex`). Image customization means changing the image used for one of those IDs.

### 1. Extend an existing image for an agent

Example: add tools to the default Claude image.

1. Create a Dockerfile that extends the current base image.

```dockerfile
# Dockerfile.claude
FROM nezhar/claude-container:latest

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

## Detached mode

Use `-d` / `--detach` to start an agent container in the background without attaching your terminal. This is useful when you want to customise the container environment before launching the agent interactively.

### Basic usage

```bash
vp run claude -d
# ✓ Started vibepod-claude-a1b2c3d4
```

The command prints the container name and returns immediately. You can also find it later with:

```bash
vp list --running
```

### Customising the container before starting the agent

A common workflow is to start detached, exec into the container to make adjustments, and then start the agent manually:

1. **Start the container in detached mode.**

    ```bash
    vp run claude -d
    # ✓ Started vibepod-claude-a1b2c3d4
    ```

2. **Exec into the running container.**

    Use the container name printed above (or grab it from `vp list`):

    ```bash
    docker exec -it vibepod-claude-a1b2c3d4 bash
    ```

3. **Apply your customisations** — install packages, edit config files, set environment variables, etc.

4. **Start the agent process** from inside the container when you are ready.

!!! tip
    If you find yourself running the same setup steps every time, consider using [`agents.<agent>.init`](#init-scripts-before-startup) commands or [extending the base image](#image-customization-workflows) instead.

### Managing detached containers

Check running agents:

```bash
vp list              # shows running + configured agents
vp list --running    # shows only running agents
vp list --json       # machine-readable output
```

Stop a specific agent or all agents:

```bash
vp stop claude       # graceful stop (10 s timeout)
vp stop claude -f    # force stop immediately
vp stop --all        # stop every VibePod container
```

### Caveats

- **`auto_remove` (default: `true`)** — By default, containers are automatically removed when they stop. This means you cannot restart a stopped detached container; you need to `vp run` again. Set `auto_remove: false` in your [configuration](../configuration.md) if you want stopped containers to persist.
- **No built-in re-attach** — VibePod does not currently have a command to re-attach your terminal to a detached container. Use `docker attach <container>` or `docker exec -it <container> bash` directly.
- **Session logging** — Sessions started with `--detach` are not recorded in the VibePod session log since VibePod does not capture the interactive I/O. If you need session logging, run without `--detach`.

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

### Gemini (Google)

```bash
vp run gemini   # or: vp g
```

### OpenCode (OpenAI)

```bash
vp run opencode   # or: vp o
```

### Devstral (Mistral)

```bash
vp run devstral   # or: vp d
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
