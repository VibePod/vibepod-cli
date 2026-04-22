<p align="center">
  <img src="https://raw.githubusercontent.com/VibePod/vibepod-cli/main/docs/assets/icon.png" alt="VibePod icon" width="150" />
</p>

<h1 align="center">VibePod</h1>

<p align="center">
  <a href="https://vibepod.dev/docs/"><img alt="Docs" src="https://img.shields.io/badge/docs-vibepod.dev-blue" /></a>
  <a href="https://pypi.org/project/vibepod/"><img alt="PyPI" src="https://img.shields.io/pypi/v/vibepod" /></a>
  <a href="https://github.com/VibePod/vibepod-cli/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/VibePod/vibepod-cli/actions/workflows/ci.yml/badge.svg" /></a>
  <a href="https://github.com/VibePod/vibepod-cli/actions/workflows/docs.yml"><img alt="Docs Build" src="https://github.com/VibePod/vibepod-cli/actions/workflows/docs.yml/badge.svg" /></a>
  <img alt="License" src="https://img.shields.io/github/license/VibePod/vibepod-cli" />
</p>

VibePod is a unified CLI (`vp`) for running AI coding agents in isolated
Docker containers — no required configuration, no setup. Just
`vp run <agent>`. Includes built-in local metrics collection, HTTP traffic
tracking, and an analytics dashboard to monitor and compare agents side-by-side.

## Features

- ⚡ **Zero config** — no setup required; `vp run <agent>` just works. Optional YAML for custom configuration
- 🐳 **Isolated agents** — each agent runs in its own Docker container
- 🔀 **Unified interface** — one CLI for Claude, Gemini, Codex, Devstral/Vibe, Copilot, Auggie & more
- 📊 **Local analytics dashboard** — track usage and HTTP traffic per agent, plus token metrics
- ⚖️ **Agent comparison** — benchmark multiple agents against each other in the dashboard
- 🔒 **Privacy-first** — all metrics collected and stored locally, never sent to the cloud
- 📦 **Simple install** — `pip install vibepod`

## Installation

VibePod is available on [PyPI](https://pypi.org/project/vibepod/):

```bash
pip install vibepod
```

## Quick Start

```bash
vp run <agent>
# examples:
vp run claude
vp run codex
vp run vibe   # alias of devstral
```

Extra arguments after the agent are forwarded to the agent process. Use `--`
before agent flags so VibePod does not parse them as its own options:

```bash
vp run <agent> -- <agent-args>
```

## IKWID Mode (`--ikwid`)

Use `--ikwid` to append each agent's auto-approval / permission-skip flag when supported.

| Agent | `--ikwid` appended args |
|---|---|
| `claude` | `--dangerously-skip-permissions` |
| `gemini` | `--approval-mode=yolo` |
| `devstral` (`vibe`) | `--auto-approve` |
| `copilot` | `--yolo` |
| `codex` | `--dangerously-bypass-approvals-and-sandbox` |
| `opencode` | Not supported |
| `auggie` | Not supported |

![VibePod CLI preview](https://raw.githubusercontent.com/VibePod/vibepod-cli/main/docs/assets/preview.png)

## Tool Thumbnails

<p>
  <a href="https://raw.githubusercontent.com/VibePod/vibepod-cli/main/docs/assets/claude%20code.png"><img src="https://raw.githubusercontent.com/VibePod/vibepod-cli/main/docs/assets/claude%20code.png" alt="Claude Code" width="180" /></a>
  <a href="https://raw.githubusercontent.com/VibePod/vibepod-cli/main/docs/assets/google%20gemini.png"><img src="https://raw.githubusercontent.com/VibePod/vibepod-cli/main/docs/assets/google%20gemini.png" alt="Google Gemini" width="180" /></a>
  <a href="https://raw.githubusercontent.com/VibePod/vibepod-cli/main/docs/assets/openai%20codex.png"><img src="https://raw.githubusercontent.com/VibePod/vibepod-cli/main/docs/assets/openai%20codex.png" alt="OpenAI Codex" width="180" /></a>
  <a href="https://raw.githubusercontent.com/VibePod/vibepod-cli/main/docs/assets/github%20copilot.png"><img src="https://raw.githubusercontent.com/VibePod/vibepod-cli/main/docs/assets/github%20copilot.png" alt="GitHub Copilot" width="180" /></a>
  <a href="https://raw.githubusercontent.com/VibePod/vibepod-cli/main/docs/assets/opencode.png"><img src="https://raw.githubusercontent.com/VibePod/vibepod-cli/main/docs/assets/opencode.png" alt="OpenCode" width="180" /></a>
  <a href="https://raw.githubusercontent.com/VibePod/vibepod-cli/main/docs/assets/mistral%20vibe.png"><img src="https://raw.githubusercontent.com/VibePod/vibepod-cli/main/docs/assets/mistral%20vibe.png" alt="Mistral Vibe" width="180" /></a>
  <a href="https://raw.githubusercontent.com/VibePod/vibepod-cli/main/docs/assets/augment%20auggie.png"><img src="https://raw.githubusercontent.com/VibePod/vibepod-cli/main/docs/assets/augment%20auggie.png" alt="Augment Auggie" width="180" /></a>
</p>

## Current Status

This repository contains an initial v1 implementation with:

- `vp run <agent>`
- `vp stop <agent|--all>`
- `vp list`
- `vp config init`
- `vp config show`
- `vp config path`
- `vp version`

## Analytics & Dashboard

VibePod collects metrics locally while your agents run and serves them through
a built-in dashboard.

![VibePod Analytics Dashboard](https://raw.githubusercontent.com/VibePod/vibepod-cli/main/docs/assets/dashboard.png)

| Command          | Description                                        |
|------------------|----------------------------------------------------|
| `vp logs start`  | Start or resume dashboard for collected metrics     |
| `vp logs stop`   | Stop the dashboard container                       |
| `vp logs status` | Show dashboard container status                    |

The dashboard shows per-agent HTTP traffic, usage over time, and Claude token
metrics. It also lets you compare agents side-by-side. All data stays on your
machine.

## Image Namespace

All agent images are published under the [`vibepod` namespace on Docker Hub](https://hub.docker.com/u/vibepod). Source Dockerfiles are in [VibePod/vibepod-agents](https://github.com/VibePod/vibepod-agents/tree/main/docker).

Current defaults:

- `claude` -> `vibepod/claude:latest`
- `gemini` -> `vibepod/gemini:latest`
- `opencode` -> `vibepod/opencode:latest`
- `devstral` (alias: `vibe`) -> `vibepod/devstral:latest`
- `auggie` -> `vibepod/auggie:latest`
- `copilot` -> `vibepod/copilot:latest`
- `codex` -> `vibepod/codex:latest`
- `datasette` -> `vibepod/datasette:latest`
- `proxy` -> `vibepod/proxy:latest` ([repo](https://github.com/VibePod/vibepod-proxy))

## Overriding Images

You can override any single image directly:

```bash
VP_IMAGE_CLAUDE=vibepod/claude:latest vp run claude
VP_IMAGE_GEMINI=vibepod/gemini:latest vp run gemini
VP_IMAGE_OPENCODE=vibepod/opencode:latest vp run opencode
VP_IMAGE_DEVSTRAL=vibepod/devstral:latest vp run devstral
VP_IMAGE_DEVSTRAL=vibepod/devstral:latest vp run vibe   # same agent/image as devstral
VP_IMAGE_AUGGIE=vibepod/auggie:latest vp run auggie
VP_IMAGE_COPILOT=vibepod/copilot:latest vp run copilot
VP_IMAGE_CODEX=vibepod/codex:latest vp run codex
VP_DATASETTE_IMAGE=vibepod/datasette:latest vp logs start
```

## License

MIT License - see [LICENSE](LICENSE) for details.
