# Quickstart

## Prerequisites

- Python 3.10+
- Docker **or** Podman (running)

### Using Podman instead of Docker

VibePod can use Podman's Docker-compatible API socket and applies the
necessary rootless user-namespace settings (`keep-id`) so workspace file
permissions work correctly.

Start Podman's Docker-compatible socket and point VibePod at it with
`DOCKER_HOST`:

=== "Linux"

    ```bash
    systemctl --user enable --now podman.socket
    export DOCKER_HOST=unix:///run/user/$(id -u)/podman/podman.sock
    ```

=== "macOS"

    ```bash
    podman machine init   # first time only
    podman machine start
    export DOCKER_HOST="unix://$(podman machine inspect --format '{{.ConnectionInfo.PodmanSocket.Path}}')"
    ```

    !!! note "The socket path is dynamic"
        The Podman machine socket lives under `$TMPDIR`, which changes between
        machine starts — a hardcoded `DOCKER_HOST` will break after a reboot.
        Always compute it via `podman machine inspect` as above, and add the
        `export` line to your `~/.zshrc` so every shell picks it up.

!!! warning "Container DNS required"
    VibePod routes agent traffic through a `vibepod-proxy` container. The agent
    resolves the proxy by container name over the `vibepod-network` network,
    which requires **DNS** to be enabled.

    If your Podman installation uses the **CNI** network backend, you must
    install the `dnsname` plugin — otherwise DNS is disabled and the proxy
    will be unreachable.

    === "Fedora / RHEL"

        ```bash
        sudo dnf install podman-plugins
        ```

    === "Ubuntu / Debian"

        ```bash
        sudo apt install golang-github-containernetworking-plugin-dnsname
        ```

    === "Arch"

        ```bash
        sudo pacman -S podman-dnsname
        ```

    After installing, recreate the network so DNS takes effect:

    ```bash
    podman network rm vibepod-network 2>/dev/null
    podman network create vibepod-network
    ```

    Verify with:

    ```bash
    podman network inspect vibepod-network --format '{{.DNSEnabled}}'
    # Should print: true
    ```

    **Alternatively**, switch to the [Netavark](https://github.com/containers/netavark)
    network backend (Podman 4.0+), which includes DNS out of the box. See
    `podman info --format '{{.Host.NetworkBackend}}'` to check your current
    backend.

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
2. Create a dedicated Docker or Podman network (`vibepod-network`).
3. Mount your current directory as the workspace inside the container.
4. Start the agent container and attach your terminal to it.

Press **Ctrl+C** to stop the container when you are done.

!!! note
    Closing the terminal window does not stop the container — the agent keeps running in the background. Use `vp list --running` to see it and `vp attach <container>` to rejoin the session. See [Reattaching a terminal](agents/index.md#reattaching-a-terminal) for details.

## Shortcuts

You can start agents with the full agent command. Most agents also have a single-letter
shortcut; Pi uses `vp pi` because `vp p` is assigned to Copilot, and Agy uses
`vp n`.

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

## Add reusable skills

Skills are reusable prompt/recipe folders that supported agents can discover at
runtime. Install one with:

```bash
vp skills add github:vibepod/vibepod-skills//skills/researcher --scope user
```

Use `--scope local` to install into the current project, or omit it to use the
normal scope auto-detection. See [Skills](skills/index.md) for scopes, GitHub
URL installs, bundles, and update/sync behavior.

Browse example skills and future VibePod-specific skills in
[`VibePod/vibepod-skills`](https://github.com/VibePod/vibepod-skills).

## View the session log UI

VibePod records every session and proxied HTTP request. Open the Datasette UI with:

```bash
vp logs start
```

This starts a Datasette container and opens `http://localhost:8001` in your browser.

## Next Steps

- [Configure an agent](agents/index.md) — set API keys and per-agent options.
- [Install skills](skills/index.md) — add reusable prompts/recipes to agents.
- [Configuration reference](configuration.md) — tune defaults, the proxy, and logging.
- [CLI Reference](cli-reference.md) — every command and flag.
