# VibePod

**One CLI for all AI coding agents — running in Docker containers.**

VibePod (`vp`) lets you run any supported AI coding agent in an isolated Docker container, pointed at any workspace directory, with a single command. Agent credentials, config, and session logs are persisted across runs without touching your host environment.

## Why VibePod?

- **One tool, all agents** — switch between Claude, Gemini, Devstral, Codex, and more without juggling separate CLIs or global installs.
- **Isolated by default** — each agent runs in its own container. No global npm packages, no credential bleed between sessions.
- **Workspace-aware** — mount any directory as the workspace at runtime. Works with monorepos, multiple projects, and Docker Compose setups.
- **Built-in observability** — session logging and an HTTP proxy are included out of the box. Browse all agent traffic in Datasette at `localhost:8001`.

## Supported Agents

| Agent | Provider | Shortcut |
|-------|----------|----------|
| `claude` | Anthropic | `vp c` |
| `gemini` | Google | `vp g` |
| `opencode` | OpenAI | `vp o` |
| `devstral` | Mistral | `vp d` |
| `auggie` | Augment Code | `vp a` |
| `copilot` | GitHub | `vp p` |
| `codex` | OpenAI | `vp x` |

## Next Steps

- [**Quickstart**](quickstart.md) — install and run your first agent in two minutes.
- [**Development**](development.md) — local setup, tests, and docs workflow.
- [**Agents**](agents/index.md) — per-agent setup and credential instructions.
- [**Configuration**](configuration.md) — full reference for global and project-level config.
- [**CLI Reference**](cli-reference.md) — every command and flag.
