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
