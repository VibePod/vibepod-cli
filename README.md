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

## Development

```bash
pip install -e ".[dev]"
vp --help
pytest
```

## Image Namespace

By default, agent images use the `nezhar` namespace (for example `nezhar/claude-container:latest`).

When images move, switch namespace without changing config files:

```bash
VP_IMAGE_NAMESPACE=vibepod vp run claude
```

Current defaults are aligned to existing container repos:

- `claude` -> `nezhar/claude-container:latest`
- `gemini` -> `nezhar/gemini-container:latest`
- `opencode` -> `nezhar/opencode-cli:latest`
- `devstral` -> `nezhar/devstral-cli:latest`
- `auggie` -> `nezhar/auggie-cli:latest`

You can override any single image directly:

```bash
VP_IMAGE_CLAUDE=nezhar/claude-container:latest vp run claude
VP_IMAGE_GEMINI=nezhar/gemini-container:latest vp run gemini
```
