# Using Podman

VibePod works with [Podman](https://podman.io/) as an alternative to Docker. Rootless Podman is fully supported — no `sudo` required.

## Prerequisites

- Podman 4.0+
- The rootless Podman socket enabled:

```bash
systemctl --user enable --now podman.socket
```

Verify the socket is active:

```bash
systemctl --user status podman.socket
```

## Selecting the runtime

VibePod auto-detects available runtimes. If only Podman is installed, it is used automatically. When both Docker and Podman are available, VibePod prompts you to choose (and remembers your choice).

You can also set the runtime explicitly using any of the methods below. They are listed in priority order — the first one found wins:

### 1. CLI flag

Pass `--runtime` to any command:

```bash
vp run claude --runtime podman
vp stop --all --runtime podman
vp logs start --runtime podman
```

### 2. Environment variable

Set `VP_CONTAINER_RUNTIME` to skip the prompt entirely:

```bash
export VP_CONTAINER_RUNTIME=podman
vp run claude
```

This is useful in CI or shell profiles where you always want a specific runtime.

### 3. Global config

Add `container_runtime` to `~/.config/vibepod/config.yaml`:

```yaml
container_runtime: podman
```

When VibePod prompts you to choose a runtime interactively, it saves your answer here automatically so you are not asked again.

Set it back to `auto` to re-enable detection:

```yaml
container_runtime: auto
```

## How it works

VibePod uses the standard [Docker SDK for Python](https://docker-py.readthedocs.io/) to communicate with Podman through its Docker-compatible REST API. No additional Python packages are needed.

The rootless Podman socket is discovered via `$XDG_RUNTIME_DIR/podman/podman.sock` (falling back to `/run/user/<uid>/podman/podman.sock`). The rootful socket at `/run/podman/podman.sock` is only used when running as root.

VibePod allows up to 10 seconds for runtime detection probes. If your Podman socket is slower on a particular host, set `VP_RUNTIME_PROBE_TIMEOUT` to a larger value in seconds before running `vp`.

## Known limitations

### Interactive attach

The `attach_socket()` call in Podman's Docker-compat layer has minor gaps under rootless mode. If you experience rendering issues while attached to a container, try running with `--detach` and using `podman attach` directly:

```bash
vp run claude -d
podman attach vibepod-claude-<id>
```

### User namespace mapping

If you want Podman to preserve your host UID/GID for compatible containers, set a user namespace mode such as `keep-id`:

```bash
vp run claude --runtime podman --userns keep-id
```

You can also set it globally:

```bash
export VP_CONTAINER_USERNS_MODE=keep-id
```

```yaml
container_userns_mode: keep-id
```

This works best for images that run as your host UID. Images that switch to a different in-container user may still produce remapped ownership on bind mounts.

### Volume permissions

Rootless Podman uses user-namespace remapping: your host UID is mapped to root inside the container, while other UIDs are mapped to subordinate ranges. Container images that drop privileges via `su` or `gosu` may encounter permission errors on bind-mounted files.

VibePod handles this automatically for its own managed directories (agent config, proxy data, CA certificates) by adjusting permissions before and during container startup. Your workspace directory is mounted as-is and is not modified.

If you still see permission errors on workspace files, adjust permissions for the owner first. For collaborative setups that share a group, you can optionally grant group write access as well. Avoid making the workspace world-writable.

```bash
chmod -R u+rwX ~/my-project
# Optional for shared group workspaces:
chmod -R g+rwX ~/my-project
```

### Rootful Podman

VibePod targets rootless Podman. The rootful socket (`/run/podman/podman.sock`) is only probed when running as root. If you need rootful Podman as a non-root user, set `DOCKER_HOST` explicitly:

```bash
export DOCKER_HOST=unix:///run/podman/podman.sock
```

Note that this will require elevated privileges (e.g. via `sudo` or polkit).
