# Agent sandboxes

Select one sandbox with `sandbox.type` in global or project configuration,
`--sandbox <type>` for one launch, or `VP_SANDBOX`. A string such as
`sandbox: gvisor` is shorthand. The default is `none`.

Precedence is CLI, environment, launched workspace configuration, then global
configuration. Per-type options live in `sandbox.gvisor` or
`sandbox.microsandbox` and are retained when selection changes.

| Type | Isolation provided | Platforms | Supported features |
| --- | --- | --- | --- |
| `none` | Normal Docker/Podman container; shared host kernel | Docker/Podman platforms | Workspace mounts, proxy/metrics, ports, attach, task, ACP, overlays, init, herdr |
| `gvisor` | User-space application kernel, with host networking; no hardware VM boundary | Rootful Docker/Podman on Linux | Same container features; herdr socket requires host Unix socket support |
| `microsandbox` | Hardware-isolated microVM via `msb` | Linux with KVM; Apple Silicon macOS | Workspace/config mounts, ports, interactive and detached run; no proxy/metrics, session logging, attach, task, ACP, overlays, init or herdr socket |

Unsupported combinations fail before launch. Microsandbox requires explicitly
disabling the proxy (`proxy.enabled: false` or `VP_PROXY_ENABLED=false`) and
session logging (`logging.enabled: false`), and
`--no-herdr` when running in an active herdr pane. Its port and mount formats
are validated before pulling or starting an agent image.

## Compatibility

`runtime: microsandbox`, `--runtime microsandbox`, and `VP_RUNTIME=microsandbox`
remain deprecated aliases. Their `docker` value maps to `none`.
`sandbox.mode` is a deprecated alias for `sandbox.type`. Each produces a
one-line warning. Canonical selection wins when both forms occur at the same
precedence level.

Check the selected type with `vp doctor sandbox`, or name one explicitly:
`vp doctor sandbox gvisor` or `vp doctor sandbox microsandbox`. The old
`vp doctor microsandbox` command is a deprecated alias. `vp version` (including
`--json`) reports the selected sandbox. Invalid selections produce a clean error.
An invalid legacy `runtime:` value warns without preventing container listing;
sandbox commands reject it before launch.

`vp attach` targets existing Docker/Podman containers regardless of the configured
sandbox for new runs. Microsandbox microVMs are not Docker/Podman containers and
cannot be attached with this command.

## none

Uses the existing Docker/Podman launch path and normal engine runtime. It
adds no isolation beyond the container engine's configuration. Agent access
to mounted files, credentials and network resources remains governed by the
normal VibePod settings.

## gVisor

VibePod can run the agent container under [gVisor](https://gvisor.dev/), an
application kernel that serves the container's system calls in user space
instead of passing them to the host kernel. It is opt-in and swaps only the
agent container's OCI runtime (`runsc`); images, the `/workspace` mount, the
proxy sidecar and published ports stay the same.

!!! warning "What gVisor does not do"
    The sandbox reduces the **host kernel** attack surface. It does not limit
    what the agent can do with what VibePod gives it: the workspace and the
    agent's config directory are still mounted read-write, credentials in the
    environment are still readable, and network egress is still open (the
    [proxy](configuration.md#the-built-in-proxy) observes and filters it, it is
    not a sandbox). Use it as defence in depth, not as a reason to trust an
    agent with more.

### Requirements

- A **Linux** host (x86_64 or ARM64, kernel 5.6+). Docker Desktop and Podman
  machine on macOS/Windows run containers in a VM without `runsc`.
- A **rootful** Docker or Podman engine. Rootless engines are rejected: runsc
  needs network and user-namespace setup that VibePod's rootless launch path
  (`keep-id`) does not provide.
- gVisor installed and `runsc` registered with the engine.

### Setup

Install gVisor from its [apt repository or release tarball](https://gvisor.dev/docs/user_guide/install/).

#### Docker

Register a runtime with two flags VibePod relies on, then restart Docker:

```bash
sudo runsc install --runtime runsc-vibepod -- --network=host --host-uds=open
sudo systemctl restart docker
```

- `--network=host` is **required**. VibePod attaches agents to the
  user-defined `vibepod-network`, where Docker serves DNS on `127.0.0.11`
  through netfilter rules that gVisor's own network stack never sees
  ([google/gvisor#7469](https://github.com/google/gvisor/issues/7469)).
  Without the flag the agent resolves no hostnames, so VibePod refuses to
  launch. Despite the name, the container keeps its own network namespace and
  stays on `vibepod-network`; gVisor uses the host kernel's TCP/IP stack
  inside that namespace instead of its user-space netstack.
- `--host-uds=open` is optional. It lets the agent connect to host unix
  sockets that are bind-mounted in, which the [herdr](herdr.md) integration
  needs for live agent state. When herdr is active, VibePod refuses the launch
  unless host sockets are enabled or you pass `--no-herdr`.

Point VibePod at the runtime name (the default is `runsc`):

```yaml
# ~/.config/vibepod/config.yaml
sandbox:
  gvisor:
    runtime: runsc-vibepod
```

You can also re-register the plain `runsc` runtime with the same flags
instead; other containers using `--runtime=runsc` then get them too.

#### Podman

Podman ships a `runsc` runtime entry in `containers.conf`; installing the
binary to `/usr/bin`, `/usr/sbin` or `/usr/local/bin` is enough. Podman's DNS
(aardvark-dns) listens on the network gateway, so the default netstack works
and no `--network=host` is needed. Podman does not report runtime flags, so
VibePod cannot see `--host-uds` there and cannot mount the herdr socket; use `--no-herdr` when herdr is active.
If `runsc` is missing, the launch fails with Podman's error and a pointer to
this page.

#### Check the setup

```bash
vp doctor sandbox
```

reports the sandbox the current project would use, the engine, the runtime's
path and flags, and whether the gVisor prerequisites are met (exit code 1 if
not).

### Enabling the sandbox

Per run:

```bash
vp run claude --sandbox gvisor
vp task create codex "fix the tests" --sandbox gvisor
```

Per project (commit it to require the sandbox for everyone on the project):

```yaml
# .vibepod/config.yaml
sandbox: gvisor
```

Globally, with `sandbox.type: gvisor` in `~/.config/vibepod/config.yaml` or
`VP_SANDBOX=gvisor`.

Precedence: `--sandbox` > `VP_SANDBOX` > the launched workspace's
`.vibepod/config.yaml` > the global config. `--sandbox none` opts a single run
out. The default is `none`, which launches exactly as before.

Sandboxed containers carry the label `vibepod.sandbox=gvisor`, and the start
line reads `Starting <agent> ... in the gvisor sandbox (runtime runsc)`.

### Errors

Every check runs before VibePod creates a network, proxy or container:

| Situation                                        | What VibePod does                              |
| ------------------------------------------------ | ---------------------------------------------- |
| Not Linux                                        | Refuses: runsc is Linux-only                   |
| Rootless Docker or Podman                        | Refuses                                        |
| Docker: runtime not registered                   | Refuses, lists registered runtimes, shows the `runsc install` command |
| Docker: runtime without `--network=host`         | Refuses (DNS would not work), shows the fix    |
| Docker: containerd shim runtime (flags unknown)  | Warns that it needs `network=host`, launches   |
| Runtime without `--host-uds=open`, or Podman | Refuses active herdr wiring; enable host sockets or pass `--no-herdr` |
| SELinux enabled on the engine                    | Adds `--security-opt label=disable` (runsc cannot apply SELinux labels) |
| Engine fails to create the container             | Names the runtime and points to `vp doctor sandbox` |

### Compatibility

| VibePod feature              | Under gVisor                                                    |
| ---------------------------- | --------------------------------------------------------------- |
| `/workspace` bind mount      | Works; host ownership and modes are preserved, writes land on the host |
| Proxy sidecar and its CA     | Works; only the agent container is sandboxed, the proxy runs under the default runtime |
| Published ports (`-p`)       | Works                                                           |
| `vp attach`, TTY, resize     | Works                                                           |
| `vp task`, `--acp`           | Work; both honour `--sandbox` / the config                      |
| herdr socket                 | Needs `--host-uds=open` on Docker; requires `--no-herdr` on Podman            |
| Agent images (Node, Python, git) | Run unmodified; gVisor implements the Linux syscall surface these tools use |
| Rootless Podman / Docker     | Not supported                                                   |
| Hermes                       | Same constraints as without the sandbox (rootful only)         |

Known gVisor behaviour to keep in mind:

- Files changed on the host while the agent runs are picked up (bind mounts
  are revalidated), but **inotify** watches inside the sandbox do not see host
  changes. Watch-mode dev servers inside the agent container may not reload
  for edits made on the host.
- The agent sees a gVisor kernel: `uname -r` and `dmesg` differ from the host,
  and a few tools that inspect `/proc` or `/sys` in depth report less.
- Nested containers (Docker-in-Docker) and kernel modules are unavailable.

### Performance

gVisor's cost is per system call, so it depends on the workload. These are
gVisor's documented characteristics, not VibePod measurements:

- **Startup** adds the sandbox boot, typically well under a second.
- **CPU-bound** work (model calls are remote; local compilation, tests)
  runs near native speed.
- **Syscall- and file-heavy** work is where it shows: `npm install`, `git
  status` on large trees, and test suites touching many small files are
  noticeably slower than under runc.

The default `systrap` platform works everywhere, including inside VMs. On bare
metal, a runtime registered with `--platform=kvm` lowers syscall overhead;
inside VMs `systrap` is usually faster than nested KVM. Compare on your own
project with:

```bash
time vp task create claude "run the test suite" --sandbox none
time vp task create claude "run the test suite" --sandbox gvisor
```

### Isolation gained

- The agent's syscalls are handled by the gVisor Sentry (a memory-safe Go
  kernel). The host kernel sees a small, seccomp-filtered set of calls from
  the sandbox, so most kernel bugs reachable from a normal container are not
  reachable from the agent.
- Filesystem access goes through a gofer that only exposes the container's
  mounts.
- With `--network=host` (required on Docker), networking uses the host
  kernel's stack inside the container's namespace, so that part of the
  kernel stays exposed. On Podman the user-space netstack covers it too.
- Nothing changes for the workspace, credentials and egress (see the warning
  above).

### Evaluation and decision (VP-144)

**Go, as an opt-in.** gVisor plugs in as an OCI runtime without changes to
images, mounts or the proxy, and it measurably shrinks the host kernel surface
for the price of syscall overhead. Its gaps are specific and detectable, so
VibePod checks them up front instead of failing mid-session:

- Docker's embedded DNS is unreachable from gVisor's netstack on user-defined
  networks; the required `--network=host` runtime flag trades the user-space
  network stack for working name resolution.
- Host unix sockets (herdr) need `--host-uds=open`; Podman exposes no flags,
  so an active herdr integration requires `--no-herdr` there.
- Rootless engines and non-Linux hosts are out of scope.
- Maintenance is limited to the checks in `vibepod.core.sandbox`: watch
  google/gvisor#7469 (dropping the `--network=host` requirement once it is
  fixed) and the Docker/Podman runtime reporting the checks rely on. The
  integration test `tests/integration/test_gvisor_sandbox.py` runs a
  sandboxed container end to end on hosts with runsc and skips elsewhere.


## microsandbox

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

### Requirements

- **Linux** with KVM: `/dev/kvm` must exist and be readable and writable by
  your user (`sudo usermod -aG kvm $USER`, then log in again). Inside a VM,
  enable nested virtualization.
- **macOS** on Apple Silicon.
- **Windows** 10 or later with Windows Hypervisor Platform (preview upstream).

### Install microsandbox

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

### Select the runtime

#### Interactive setup

```bash
vp config init -i
```

Prompts for the runtime (`none`, `gvisor` or `microsandbox`) and, for microsandbox,
the vCPUs and memory of each microVM, then writes `.vibepod/config.yaml`.

#### Non-interactive setup (CI)

```bash
vp config init --sandbox microsandbox --msb-cpus 2 --msb-memory 4G
```

| Option         | Meaning                                         |
| -------------- | ----------------------------------------------- |
| `--sandbox`    | `none`, `gvisor` or `microsandbox`                      |
| `--msb-binary` | Name or path of the `msb` CLI (default: `msb`)  |
| `--msb-cpus`   | vCPUs per microVM (positive integer)            |
| `--msb-memory` | Memory per microVM, e.g. `512M`, `4G`           |

When `.vibepod/config.yaml` already exists, the runtime settings are merged
into it; nothing else in the file changes. `vp config init <agent> --sandbox
microsandbox` adds an agent block and the runtime in one step. Setup does not
fail when `msb` is not installed yet (so CI can write the config first); it
prints a warning and points at `vp doctor sandbox microsandbox`.

#### Per run or per shell

```bash
vp run claude --sandbox microsandbox    # this run only
export VP_SANDBOX=microsandbox          # every run in this shell
vp run claude --sandbox none          # back to Docker for one run
```

The `--sandbox` flag wins over `VP_SANDBOX`, which wins over the config files.

### Configuration

```yaml
# .vibepod/config.yaml or ~/.config/vibepod/config.yaml
sandbox:
  type: microsandbox
  microsandbox:
    binary: msb           # name on PATH or a path to the msb CLI
    cpus: 2               # vCPUs per microVM
    memory: 4G            # memory per microVM; a bare number means MiB
    args: []              # extra msb run flags
proxy:
  enabled: false
logging:
  enabled: false

```

Unknown keys and invalid values are rejected with the offending key named,
both by `vp run` and `vp doctor sandbox microsandbox`.

### How a run maps onto msb

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

### Limitations

These need Docker and are rejected with the microsandbox runtime:

| Feature                                  | Behavior                                    |
| ---------------------------------------- | ------------------------------------------- |
| `--acp`, `--network`, `--paste-images`   | Rejected before anything starts             |
| `agents.<agent>.init` commands           | Rejected; bake setup into the image instead |
| Project overlays (`.vibepod/overlay/`)   | Rejected; pass `--no-overlay` to skip       |
| `vp task`                                | Rejected; run it with `VP_SANDBOX=none`   |
| Built-in proxy and its traffic dashboard | Rejected; set `proxy.enabled: false`         |
| Session logging | Rejected; set `logging.enabled: false` |
| herdr socket | Rejected when active; pass `--no-herdr` |
| `vp list`, `vp stop`, `vp attach`        | Docker only; use `msb ls/stop/attach`        |

`host.docker.internal` is not mapped inside microVMs. To reach a model
server on the host, see microsandbox's
[network profiles](https://docs.microsandbox.dev/networking/overview)
(for example `sandbox.microsandbox.args: ["--net", "public,host"]`).

### Troubleshooting

| Message                                        | Fix                                                        |
| ---------------------------------------------- | ---------------------------------------------------------- |
| `microsandbox CLI 'msb' not found on PATH`     | Install microsandbox or set `sandbox.microsandbox.binary`          |
| `/dev/kvm is missing`                          | Enable virtualization / nested virtualization, load `kvm`  |
| `/dev/kvm is not accessible`                   | Add your user to the `kvm` group and log in again          |
| `microsandbox on macOS requires Apple Silicon` | Use the Docker runtime on Intel Macs                       |
| `Invalid runtime value '…'`                    | Use `none`, `gvisor` or `microsandbox`                             |

`vp run` exits with code 3 when the runtime is unavailable, as it does when
Docker is not running.
