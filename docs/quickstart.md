# Quickstart

## Prerequisites

- Python 3.10+
- Docker (running)

## Install

```bash
pip install vibepod
```

Verify the installation:

```bash
vp version
```

## Run your first agent

Navigate to the project you want to work on, then run an agent:

```bash
cd ~/my-project
vp run claude
```

VibePod will:

1. Pull the agent image if not already present.
2. Create a dedicated Docker network (`vibepod-network`).
3. Mount your current directory as the workspace inside the container.
4. Start the agent container and attach your terminal to it.

Press **Ctrl+C** to stop the container when you are done.

!!! note
    Closing the terminal window does not stop the container — the agent keeps running in the background. Use `vp list --running` to see it and `vp attach <container>` to rejoin the session. See [Reattaching a terminal](agents/index.md#reattaching-a-terminal) for details.

## Shortcuts

You can start agents with either the full name or a single-letter shortcut:

```bash
vp claude   # full name
vp c        # shortcut
vp run c    # shortcut with run also works
```

## Point at a different workspace

Use `-w` / `--workspace` to target any directory:

```bash
vp run claude -w ~/other-project
```

## Pass arguments to the agent

Arguments after the agent name are forwarded to the agent command inside the container:

```bash
vp run <agent> <agent-args>
```

When forwarding flags to the agent, use `--` to stop VibePod option parsing:

```bash
vp run <agent> -- <agent-flag> <value>
```

## Bootstrap a project config

Create a project-level config file that you can extend later:

```bash
vp config init
```

This creates `.vibepod/config.yaml` in your current directory with a minimal starter:

```yaml
version: 1
```

Add a specific agent block into the project config with:

```bash
vp config init claude
```

If that agent is already configured under `agents`, the command exits without changing the file.

## Run in the background

Use `-d` / `--detach` to start the container without attaching your terminal:

```bash
vp run claude -d
```

Check which agents are running:

```bash
vp list --running
```

Stop it later with:

```bash
vp stop claude
```

For more details on detached mode workflows, see [Agents > Detached mode](agents/index.md#detached-mode).

## View the session log UI

VibePod records every session and proxied HTTP request. Open the Datasette UI with:

```bash
vp logs start
```

This starts a Datasette container and opens `http://localhost:8001` in your browser.

## Next Steps

- [Configure an agent](agents/index.md) — set API keys and per-agent options.
- [Configuration reference](configuration.md) — tune defaults, the proxy, and logging.
- [CLI Reference](cli-reference.md) — every command and flag.
