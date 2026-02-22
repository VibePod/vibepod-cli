# VibePod

VibePod is a unified CLI (`vp`) for running AI coding agents in Docker containers.

## Current Status

This repository contains an initial v1 implementation with:

- `vp run <agent>`
- `vp stop <agent|--all>`
- `vp list`
- `vp logs ui`
- `vp config show`
- `vp config path`
- `vp version`

## Image Namespace

By default, agent images use the `nezhar` namespace (for example `nezhar/claude-container:latest`).


Current defaults are aligned to existing container repos:

- `claude` -> `nezhar/claude-container:latest` ([repo](https://github.com/nezhar/claude-container))
- `gemini` -> `nezhar/gemini-container:latest` ([repo](https://github.com/nezhar/gemini-container))
- `opencode` -> `nezhar/opencode-cli:latest` ([repo](https://github.com/nezhar/opencode-container))
- `devstral` -> `nezhar/devstral-cli:latest` ([repo](https://github.com/nezhar/devstral-container))
- `auggie` -> `nezhar/auggie-cli:latest` ([repo](https://github.com/nezhar/auggie-container))
- `copilot` -> `nezhar/copilot-cli:latest` ([repo](https://github.com/nezhar/copilot-container))
- `codex` -> `nezhar/codex-cli:latest` ([repo](https://github.com/nezhar/codex-container))

You can override any single image directly:

```bash
VP_IMAGE_CLAUDE=nezhar/claude-container:latest vp run claude
VP_IMAGE_GEMINI=nezhar/gemini-container:latest vp run gemini
VP_IMAGE_OPENCODE=nezhar/opencode-cli:latest vp run opencode
VP_IMAGE_DEVSTRAL=nezhar/devstral-cli:latest vp run devstral
VP_IMAGE_AUGGIE=nezhar/auggie-cli:latest vp run auggie
VP_IMAGE_COPILOT=nezhar/copilot-cli:latest vp run copilot
VP_IMAGE_CODEX=nezhar/codex-cli:latest vp run codex
VP_DATASETTE_IMAGE=nezhar/opencode-datasette:latest vp logs ui
```
