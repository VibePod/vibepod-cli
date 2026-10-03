# Agent sandbox: gVisor

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

## Requirements

- A **Linux** host (x86_64 or ARM64, kernel 5.6+). Docker Desktop and Podman
  machine on macOS/Windows run containers in a VM without `runsc`.
- A **rootful** Docker or Podman engine. Rootless engines are rejected: runsc
  needs network and user-namespace setup that VibePod's rootless launch path
  (`keep-id`) does not provide.
- gVisor installed and `runsc` registered with the engine.

## Setup

Install gVisor from its [apt repository or release tarball](https://gvisor.dev/docs/user_guide/install/).

### Docker

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
  needs for live agent state. Without it VibePod skips the herdr socket mount
  and reports pane identity from the host only.

Point VibePod at the runtime name (the default is `runsc`):

```yaml
# ~/.config/vibepod/config.yaml
sandbox:
  gvisor:
    runtime: runsc-vibepod
```

You can also re-register the plain `runsc` runtime with the same flags
instead; other containers using `--runtime=runsc` then get them too.

### Podman

Podman ships a `runsc` runtime entry in `containers.conf`; installing the
binary to `/usr/bin`, `/usr/sbin` or `/usr/local/bin` is enough. Podman's DNS
(aardvark-dns) listens on the network gateway, so the default netstack works
and no `--network=host` is needed. Podman does not report runtime flags, so
VibePod cannot see `--host-uds` there and always skips the herdr socket mount.
If `runsc` is missing, the launch fails with Podman's error and a pointer to
this page.

### Check the setup

```bash
vp doctor sandbox
```

reports the sandbox the current project would use, the engine, the runtime's
path and flags, and whether the gVisor prerequisites are met (exit code 1 if
not).

## Enabling the sandbox

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

Globally, with `sandbox.mode: gvisor` in `~/.config/vibepod/config.yaml` or
`VP_SANDBOX=gvisor`.

Precedence: `--sandbox` > `VP_SANDBOX` > the launched workspace's
`.vibepod/config.yaml` > the global config. `--sandbox none` opts a single run
out. The default is `none`, which launches exactly as before.

Sandboxed containers carry the label `vibepod.sandbox=gvisor`, and the start
line reads `Starting <agent> ... in the gvisor sandbox (runtime runsc)`.

## Errors

Every check runs before VibePod creates a network, proxy or container:

| Situation                                        | What VibePod does                              |
| ------------------------------------------------ | ---------------------------------------------- |
| Not Linux                                        | Refuses: runsc is Linux-only                   |
| Rootless Docker or Podman                        | Refuses                                        |
| Docker: runtime not registered                   | Refuses, lists registered runtimes, shows the `runsc install` command |
| Docker: runtime without `--network=host`         | Refuses (DNS would not work), shows the fix    |
| Docker: containerd shim runtime (flags unknown)  | Warns that it needs `network=host`, launches   |
| Runtime without `--host-uds=open`, or Podman     | Skips the herdr socket mount with a warning    |
| SELinux enabled on the engine                    | Adds `--security-opt label=disable` (runsc cannot apply SELinux labels) |
| Engine fails to create the container             | Names the runtime and points to `vp doctor sandbox` |

## Compatibility

| VibePod feature              | Under gVisor                                                    |
| ---------------------------- | --------------------------------------------------------------- |
| `/workspace` bind mount      | Works; host ownership and modes are preserved, writes land on the host |
| Proxy sidecar and its CA     | Works; only the agent container is sandboxed, the proxy runs under the default runtime |
| Published ports (`-p`)       | Works                                                           |
| `vp attach`, TTY, resize     | Works                                                           |
| `vp task`, `--acp`           | Work; both honour `--sandbox` / the config                      |
| herdr socket                 | Needs `--host-uds=open` on Docker; skipped on Podman            |
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

## Performance

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

## Isolation gained

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

## Evaluation and decision (VP-144)

**Go, as an opt-in.** gVisor plugs in as an OCI runtime without changes to
images, mounts or the proxy, and it measurably shrinks the host kernel surface
for the price of syscall overhead. Its gaps are specific and detectable, so
VibePod checks them up front instead of failing mid-session:

- Docker's embedded DNS is unreachable from gVisor's netstack on user-defined
  networks; the required `--network=host` runtime flag trades the user-space
  network stack for working name resolution.
- Host unix sockets (herdr) need `--host-uds=open`; Podman exposes no flags,
  so the herdr socket is skipped there.
- Rootless engines and non-Linux hosts are out of scope.
- Maintenance is limited to the checks in `vibepod.core.sandbox`: watch
  google/gvisor#7469 (dropping the `--network=host` requirement once it is
  fixed) and the Docker/Podman runtime reporting the checks rely on. The
  integration test `tests/integration/test_gvisor_sandbox.py` runs a
  sandboxed container end to end on hosts with runsc and skips elsewhere.
