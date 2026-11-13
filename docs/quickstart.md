# Quickstart

!!! tip "Already running an agent on your machine?"
    `vp import` copies an existing Claude Code, opencode or Codex setup —
    settings, models, hooks, skills and memory files — into a VibePod profile.
    See [Importing an existing setup](import.md).

## Prerequisites

- Python 3.10+
- Docker **or** Podman (running)

### Using Podman instead of Docker

VibePod can use Podman's Docker-compatible API socket and applies the
necessary rootless user-namespace settings (`keep-id`) so workspace file
permissions work correctly.

If `DOCKER_HOST` is not set, VibePod asks Podman for its socket location and
uses it automatically — on macOS this matters because the machine socket path
changes with `$TMPDIR` at machine-start time. You only need to make sure the
socket exists:

=== "Linux"

    ```bash
    systemctl --user enable --now podman.socket
    ```

=== "macOS"

    ```bash
    podman machine init   # first time only
    podman machine start
    ```

Setting `DOCKER_HOST` explicitly still works and takes precedence:

```bash
# Linux
export DOCKER_HOST=unix:///run/user/$(id -u)/podman/podman.sock
# macOS
export DOCKER_HOST=unix://$(podman machine inspect --format '{{.ConnectionInfo.PodmanSocket.Path}}')
```

!!! note "The socket path is dynamic on macOS"
    The Podman machine socket lives under `$TMPDIR`, which changes between
    machine starts — a hardcoded `DOCKER_HOST` will break after a reboot. If
    you set it manually, always compute it via `podman machine inspect` as
    above (e.g. in your `~/.zshrc`).

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

### SELinux

On an SELinux-enforcing Linux host (Fedora, RHEL, …) a bind mount keeps its
host label, which containers may not access: the agent can find its workspace
unwritable and the proxy can fail with `Permission denied`. VibePod does not
relabel anything by default. Instead, each `vp run` / `vp task create` on an
enforcing host prints a hint, and you can opt in to relabeling in
`~/.config/vibepod/config.yaml`:

```yaml
selinux_relabel: true
```

or for one command with `VP_SELINUX_RELABEL=true vp run …`
(`VP_SELINUX_RELABEL=false` turns it off again whatever the config says).

!!! warning "Relabeling is permanent"
    With `selinux_relabel` enabled VibePod adds the shared `z` option to the
    bind mounts it owns, and the container engine changes the SELinux label of
    those host files to `container_file_t`. The new label stays after the
    container exits and VibePod cannot undo it (`restorecon -R <dir>` restores
    the policy default). A host service that reads the same files — say a web
    server serving a workspace under `/var/www` — may lose access to them.
    Only enable it when your workspaces are used by you, not by host services.

When enabled, VibePod relabels the bind sources it owns as containers use
them: the workspace, the agent config directories, the directories of the
proxy CA (`proxy.ca_dir`) and of the proxy and log databases
(`proxy.db_path`, `logging.db_path`), installed skills (user and a project's
`.vibepod/skills`) and, for board tasks, the repository's git directory that a
worktree points into. By default these live under `~/.config/vibepod` or the
workspace, but the configured paths can point elsewhere, and are relabeled
there. VibePod prints the relabeled host paths when it starts a container.

`selinux_relabel` is read like any other key, so a project's
`.vibepod/config.yaml` can enable it too: review it before running an agent in
a project you did not write. It still never relabels:

- a workspace that is your home directory, one of its ancestors, or a system
  directory such as `/`, `/tmp`, `/usr`, `/etc` or `/var` — it prints a warning
  instead and the agent may be unable to access it; pick a project directory;
- anything under system trees such as `/etc` or `/usr`;
- your own `agents.<agent>.volumes` / `-v` mounts — add `z`/`Z` to their mode
  yourself (see [Mounting volumes](configuration.md#mounting-volumes));
- host-owned files such as the herdr binary and socket or the X11 socket;
- anything on a read-only filesystem, such as a VM's read-only share of the
  host home, where the engine could not write the label anyway.

Detection reads `/sys/fs/selinux/enforce` on the machine running `vp`, so a
remote `DOCKER_HOST` whose engine runs on a different (SELinux) host is not
detected.

### Native Windows

`vp` runs natively on Windows against Docker Desktop or Podman Desktop (their
default `npipe://` connection). Use Windows Terminal or the console host; the
console is switched to raw input while an agent is attached and restored
afterwards.

!!! warning "Known keyboard limitations on native Windows"
    Console input is still read key by key and translated to terminal
    sequences, so a few keys do not reach the agent correctly yet: `à` can
    swallow the next key (or turn `àH` into Arrow Up), emoji arrive as `??`,
    and Alt+key combinations are ignored. Typing everything else, including
    arrows, Home/End, PgUp/PgDn, F1–F12, Shift+Tab and Ctrl+arrows, works.
    The rewrite is tracked in [#242](https://github.com/VibePod/vibepod-cli/issues/242).
    Running `vp` inside WSL2 avoids these limitations.

## Install

=== "pip"

    ```bash
    pip install vibepod
    ```

=== "uv"

    ```bash
    uv tool install vibepod
    ```

=== "Homebrew"

    ```bash
    brew install vibepod/vibepod/vibepod
    ```

=== "conda / mamba"

    ```bash
    conda install -c conda-forge vibepod
    # or
    mamba install -c conda-forge vibepod
    ```

=== "pixi"

    ```bash
    pixi global install vibepod
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
shortcut; Pi uses `vp pi` because `vp p` is assigned to Copilot, Agy uses
`vp n`, Tau uses `vp t`, Jcode uses `vp j`, Freebuff uses `vp fb`,
Qwen uses `vp q`, Cline uses `vp cl`, Junie uses `vp ju`, Cursor uses `vp cu`, and Warp uses `vp w`. DeepSeek Harness uses `vp ds` and opens a Web UI — the URL
is printed after start.

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
