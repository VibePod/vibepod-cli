# microsandbox runtime

VibePod runs agents in Docker or Podman containers by default. With the
microsandbox runtime it boots the same agent images as hardware-isolated
microVMs through [microsandbox](https://microsandbox.dev) instead (the
`microsandbox` npm package, formerly published as
`@superradcompany/microsandbox`). Each agent gets its own kernel, so a
compromised agent cannot reach the host through a shared container kernel.

VibePod drives the `msb` CLI; no Docker daemon is needed for `vp run`.

!!! warning "Preview"
    microsandbox itself is beta software, and the VibePod integration covers
    `vp run` only. See [Limitations](#limitations).

## Requirements

- **Linux** with KVM: `/dev/kvm` must exist and be readable and writable by
  your user (`sudo usermod -aG kvm $USER`, then log in again). Inside a VM,
  enable nested virtualization.
- **macOS** on Apple Silicon.
- **Windows** 10 or later with Windows Hypervisor Platform (preview upstream).

## Install microsandbox

```bash
curl -fsSL https://install.microsandbox.dev | sh      # macOS / Linux
brew install superradcompany/tap/microsandbox          # Homebrew
npm i -g microsandbox                                  # npm
```

The install script links `msb` into `~/.local/bin`; VibePod finds it there
even when that directory is not on your `PATH`.

Check the host:

```bash
vp doctor microsandbox
```

It validates the VibePod config, the host virtualization prerequisites and
the `msb` binary, then runs `msb doctor` and prints its findings.

## Select the runtime

### Interactive setup

```bash
vp config init -i
```

Prompts for the runtime (`docker` or `microsandbox`) and, for microsandbox,
the vCPUs and memory of each microVM, then writes `.vibepod/config.yaml`.

### Non-interactive setup (CI)

```bash
vp config init --runtime microsandbox --msb-cpus 2 --msb-memory 4G
```

| Option         | Meaning                                         |
| -------------- | ----------------------------------------------- |
| `--runtime`    | `docker` or `microsandbox`                      |
| `--msb-binary` | Name or path of the `msb` CLI (default: `msb`)  |
| `--msb-cpus`   | vCPUs per microVM (positive integer)            |
| `--msb-memory` | Memory per microVM, e.g. `512M`, `4G`           |

When `.vibepod/config.yaml` already exists, the runtime settings are merged
into it; nothing else in the file changes. `vp config init <agent> --runtime
microsandbox` adds an agent block and the runtime in one step. Setup does not
fail when `msb` is not installed yet (so CI can write the config first); it
prints a warning and points at `vp doctor microsandbox`.

### Per run or per shell

```bash
vp run claude --runtime microsandbox    # this run only
export VP_RUNTIME=microsandbox          # every run in this shell
vp run claude --runtime docker          # back to Docker for one run
```

The `--runtime` flag wins over `VP_RUNTIME`, which wins over the config files.

## Configuration

```yaml
# .vibepod/config.yaml or ~/.config/vibepod/config.yaml
runtime: microsandbox   # docker (default) | microsandbox

microsandbox:
  binary: msb           # name on PATH or a path to the msb CLI
  cpus: 2               # vCPUs per microVM (default: msb's default)
  memory: 4G            # memory per microVM; a bare number means MiB
  args: []              # extra `msb run` flags, e.g. ["--net", "public"]
```

Unknown keys and invalid values are rejected with the offending key named,
both by `vp run` and `vp doctor microsandbox`.

## How a run maps onto msb

`vp run claude` with the microsandbox runtime runs, in effect:

```bash
msb run --runtime-conf <private file> \
  -v "$PWD:/workspace" -v ~/.config/vibepod/agents/claude:/claude \
  vibepod/claude:latest -- claude
```

- The workspace is mounted at `/workspace` and the agent's config directory at
  the same path as with Docker, so credentials and sessions are shared
  between both runtimes.
- Environment variables (including API keys), labels and the working
  directory go into a `--runtime-conf` file readable only by you, and deleted
  when `msb` returns, never onto the command line where other users could
  read them from the process list.
- `agents.<agent>.volumes`, `-v/--volume`, `agents.<agent>.ports` and
  `-p/--publish` keep their Docker syntax. Host directories become `-v`,
  host files `--mount-file`, named volumes `--mount-named`. Only TCP ports
  with an explicit host port can be published.
- `--ikwid`, `--provider`, `--profile`, `--pull`, LLM settings and
  passthrough arguments work as with Docker.
- Interactive runs are ephemeral: msb removes the sandbox on exit. With
  `--name` (or `auto_remove: false`) the sandbox is named; a named sandbox is
  removed after exit when `auto_remove` is on.
- `-d/--detach` starts a named sandbox in the background; manage it with
  `msb ls`, `msb logs <name>`, `msb stop <name>` and `msb rm <name>`.

## Limitations

These need Docker and are rejected, or skipped, with the microsandbox runtime:

| Feature                                  | Behavior                                    |
| ---------------------------------------- | ------------------------------------------- |
| `--acp`, `--network`, `--paste-images`   | Rejected before anything starts             |
| `agents.<agent>.init` commands           | Rejected; bake setup into the image instead |
| Project overlays (`.vibepod/overlay/`)   | Rejected; pass `--no-overlay` to skip       |
| `vp task`                                | Rejected; run it with `VP_RUNTIME=docker`   |
| Built-in proxy and its traffic dashboard | Skipped                                     |
| Session logging and herdr wiring         | Skipped                                     |
| `vp list`, `vp stop`, `vp attach`        | Docker only; use `msb ls/stop/attach`        |

`host.docker.internal` is not mapped inside microVMs. To reach a model
server on the host, see microsandbox's
[network profiles](https://docs.microsandbox.dev/networking/overview)
(for example `microsandbox.args: ["--net", "public,host"]`).

## Troubleshooting

| Message                                        | Fix                                                        |
| ---------------------------------------------- | ---------------------------------------------------------- |
| `microsandbox CLI 'msb' not found on PATH`     | Install microsandbox or set `microsandbox.binary`          |
| `/dev/kvm is missing`                          | Enable virtualization / nested virtualization, load `kvm`  |
| `/dev/kvm is not accessible`                   | Add your user to the `kvm` group and log in again          |
| `microsandbox on macOS requires Apple Silicon` | Use the Docker runtime on Intel Macs                       |
| `Invalid runtime value '…'`                    | Use `docker` or `microsandbox`                             |

`vp run` exits with code 3 when the runtime is unavailable, as it does when
Docker is not running.
