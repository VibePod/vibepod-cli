# Docker Sandboxes (sbx): analysis (no-go)

VibePod does **not** offer a Docker Sandboxes sandbox type. This page explains
why. It records the decision on VP-148, so the question can be reopened if the
constraints below change. It is based on the
[Docker Sandboxes documentation](https://docs.docker.com/ai/sandboxes/) and the
[`sbx` CLI reference](https://docs.docker.com/reference/cli/sbx/) as of `sbx`
0.46.0 (2026-09-28).

**Decision: no-go.** Docker Sandboxes is a full agent runner, not an OCI
runtime that VibePod could plug in under its own containers. Its sandbox
contract (image layout, mounts, networking, credentials, lifecycle) differs from
VibePod's on every point, so a backend would have to rewrite every VibePod agent
image and drop the proxy, the config mounts and the herdr integration. The CLI
is also pre-1.0 and ships breaking changes almost every week. Users who want VM
isolation should use the Kata sandbox (VP-145). Users who want Docker
Sandboxes' own features can run `sbx` directly, next to VibePod.

## What it is

Docker Sandboxes runs AI coding agents in isolated environments, either locally
or on Docker-managed cloud compute, driven by its own `sbx` CLI and an `sbx`
daemon (`sandboxd`). A local sandbox is:

- a **microVM** with its own Linux kernel, started through the host hypervisor
  (Apple Virtualization on macOS, Windows Hypervisor Platform, KVM on Linux);
- its own **Docker Engine** inside the VM, so the agent can build and run
  containers without access to the host daemon;
- one container image (a *template*, by default
  `docker/sandbox-templates:<agent>`) that provides the agent's userland;
- a host-side **proxy** that every outbound TCP connection goes through, which
  enforces a network policy (Open / Balanced / Locked Down plus allow and deny
  rules) and injects API credentials into requests, so keys never enter the VM;
- a host workspace shared through a virtiofs passthrough at the **same absolute
  path** as on the host, or a read-only mount plus a private clone
  (`--clone`), or no workspace at all.

Sandboxes are persistent: `sbx run` creates the sandbox if needed and attaches,
`sbx stop` keeps it, and `sbx rm` deletes the VM and everything in it.

## Platforms and licensing

| Host | Local sandboxes |
| --- | --- |
| macOS 14+ on Apple silicon | Yes |
| macOS on Intel | No |
| Windows 11, x86_64, Windows Hypervisor Platform enabled | Yes |
| Windows on ARM, Windows 10 | No |
| Ubuntu 24.04+ (x86_64, arm64), KVM and user in the `kvm` group | Yes |
| Other Linux distributions | Not tested or supported by Docker (release artifacts exist) |
| Linux VM / cloud instance | Only with nested virtualisation |
| GitHub-hosted CI runners | Not a documented target |

- Docker Desktop is **not** required; `sbx` ships its own daemon and VM image
  store. Docker Engine is optional.
- The CLI and local sandboxes are free, including for commercial use. Cloud
  sandboxes are pay-as-you-go; organisation policies and audit logs need a
  separate paid subscription.
- **A Docker account sign-in (`sbx login`, browser OAuth) is mandatory**, also
  for local sandboxes. The CLI sends usage telemetry with the Docker username
  unless `SBX_NO_TELEMETRY=1` is set.

VibePod supports Docker Engine, Docker Desktop and Podman (rootful and
rootless) on Linux, macOS (Intel and Apple silicon) and Windows, with no
account. A Docker Sandboxes backend would only be available to a subset of
them, and only after they sign in to Docker.

## Stability of the CLI

The CLI is at 0.x and releases about once a week. Three of the four releases
from 0.43.0 to 0.46.0 (2026-09-15 to 2026-09-28) list breaking changes (for
example the skills-sharing default and the removal of `sbx mcp catalog`). Kits,
`sbx env`, `sbx setup`, `sbx skills` and upstream proxy support are marked
experimental. Kit authoring for custom images is early access. A VibePod
backend would depend on CLI flags and output formats that may change without
notice. `sbx` has no documented machine-readable API for local sandboxes
(only `--json` on a few commands); the Sandboxes API and SDK target cloud
sandboxes.

## Integration shapes

Docker Sandboxes does not register an OCI runtime with Docker or Podman, so the
gVisor-style shape (swap `--runtime` and keep everything else) is not
available. The only shape is a **separate backend** that replaces the container
engine calls in `core/docker.py` with `sbx create` / `sbx run` / `sbx exec`.

### Can it run VibePod's agent images?

Not without changes. `sbx run -t <image>` accepts any image, but the image must
meet the sandbox contract:

| Docker Sandboxes requires | VibePod agent images today |
| --- | --- |
| A non-root `agent` user with UID 1000 and home `/home/agent`, set as `USER` | Start as root; the `vibepod-agents` entrypoint maps `USER_UID`/`USER_GID` to the host user, or keeps root |
| `/bin/bash`; the agent is launched through non-interactive Bash, and `sbx` keeps the sandbox running itself | Some images are Alpine/BusyBox without Bash (see the Gemini launcher in `core/agents.py`); the entrypoint is PID 1 and the container's lifetime |
| Images are pulled from a registry or loaded with `sbx template load` into the sandbox's own image store | Overlays and profiles are built locally with the host engine (`vp` builds the image, then runs it) |

Every agent image (and each overlay build) would need a second variant, or a
reworked entrypoint, that follows the `sbx` user and launch contract, plus a
`docker save | sbx template load` step for local builds.

### Env, mounts, proxy and ports

| VibePod feature | Under Docker Sandboxes |
| --- | --- |
| `-e` / per-agent env | Yes: `sbx create -e` / `--env-file`. But credentials passed as env sit inside the VM, which bypasses the credential isolation that is the main point of `sbx`. |
| `/workspace` bind mount | Not at `/workspace`: the workspace appears at its host absolute path. Agent commands, ACP path parity and docs that assume `/workspace` would change. |
| Agent config dir (`/claude`, `/config`, …) | No arbitrary mount targets. Extra paths can be shared only at their host path (`sbx run agent . /path`), so every `config_mount_path` contract would need a per-agent env rewrite. |
| Proxy CA, skills, overlays, custom `-v` volumes | Same limitation: no target paths; read-only extras only at the host path. |
| `vibepod-network`, `vibepod-proxy` DNS name | No. Each sandbox has its own network and cannot join a Docker network. The agent's traffic goes through the `sbx` host proxy, which already intercepts TLS for credential injection. |
| VibePod proxy (traffic log, metrics, egress filter, per-launch identity) | Only as a **global** `sbx settings set proxy.sandbox` upstream (experimental), applied to all sandboxes. It would have to be published on a host port, the per-launch `vp-<id>` proxy credentials would be lost (the `sbx` proxy makes the upstream connection), and the VibePod CA would have to be trusted by the `sbx` proxy, not the agent. Per-agent metrics and policy attribution would not work. |
| `host.docker.internal` (local LLM servers) | Reachable only through a network policy rule for host services. |
| Published ports (`--port`, Web UI agents) | Yes: `sbx create -p` and `sbx ports`. Default host bind is loopback. |
| herdr socket | No: `sbx` forwards only SSH agent sockets. |
| Rootless Podman `keep-id`, SELinux labels | Not applicable; `sbx` manages its own VM. |

### `vp attach`, `vp logs`, `vp list`, the dashboard and board workers

- **Attach:** `sbx run --name <sandbox>` re-attaches to the agent; detach is
  `Ctrl-\`. `vp attach` uses the Docker attach stream and would need a
  subprocess wrapper. Terminal size and the session log tee (`vp run` records
  the agent's output) would depend on `sbx`'s own PTY handling.
- **Logs:** `sbx` has no `logs` command. VibePod's session logging, and the
  proxy traffic the dashboard (`vp logs`, Datasette) shows, would be lost or
  reduced to what the `sbx` policy log records.
- **List/stop:** sandboxes are not Docker containers, so `vp list`, `vp stop`
  and the `vibepod.*` labels would need a second code path over `sbx ls`
  (no labels; names only).
- **Tasks and board workers:** `vp task` and board workers depend on container
  exit codes, labels and the proxy filter. `sbx run` refuses non-interactive
  runs without `--detached`, and `sbx exec` has no detached mode, so headless
  tasks would have to be rebuilt around `sbx create` + `sbx exec`.
- **ACP:** `sbx exec -i` could carry stdio, but path parity would rely on the
  workspace host path, and `sbx` starts the agent itself on `sbx run`.

## Isolation gained

Docker Sandboxes gives more isolation than VibePod's default container:

- **Hypervisor boundary:** a separate guest kernel per sandbox. A kernel or
  container-escape exploit stays in the VM. This is the same kind of boundary
  as Kata Containers.
- **Network policy by default:** deny-by-default presets, DNS through a
  policy-enforcing resolver, UDP and ICMP blocked. VibePod's proxy observes
  and filters HTTP(S) but does not stop raw TCP.
- **Credential isolation:** API keys stay on the host and are injected into
  requests by the host proxy, so the agent cannot read them. VibePod passes
  credentials as environment variables.
- **Docker inside the sandbox:** the agent can run `docker build` against an
  in-VM engine with no path to the host daemon.
- **Clone mode:** the agent works on a private clone, so it cannot touch host
  `.git` hooks or the working tree.

What it does not change: in direct-mount mode, the agent still has read-write
access to the workspace, including `.git/hooks`, CI files and agent settings.
Inside the VM it runs with passwordless `sudo`. SSH agent forwarding is on by
default, and host clipboard writes are allowed.

Most of these gains come from `sbx`'s own proxy, credential store and policy
engine, not from the microVM. A VibePod backend would have to either replace
VibePod's proxy and env-based credentials with them (and lose VibePod's
traffic log, dashboard and filters) or work around them (and lose the gains).
The microVM boundary alone is available through Kata without these trade-offs.

## Overhead

- **Startup:** the first run of an agent pulls its template into the `sbx`
  image store; later starts take seconds. Sandboxes persist, so restarts reuse
  the VM.
- **Memory:** by default each sandbox gets 50% of host memory (512 MiB to
  32 GiB) and all host CPUs. Running several agents in parallel (board workers)
  needs explicit `--memory` / `--cpus`.
- **Disk:** each sandbox has its own VM image, Docker image cache and package
  installs, with no layer sharing between sandboxes or with the host engine.
  VibePod images would be stored twice (host engine and `sbx` store).
- **Workspace I/O:** virtiofs with host-side caching. It is close to a bind
  mount for most workloads, but slower than a native bind mount on Linux.

## Where VibePod adds value on top

Docker Sandboxes and VibePod overlap (isolated agent runs, per-agent images,
network control), but VibePod adds:

- maintained images for 15 agents, including ones `sbx` does not ship
  (Hermes, Pi, Qwen, Mistral Vibe, Auggie, …), with overlays, profiles and model providers;
- the proxy traffic log, per-launch metrics and the Datasette dashboard;
- `vp task` and board workers for unattended runs;
- ACP editor integration and herdr pane state;
- support for Podman, Docker Engine and Docker Desktop on any OS, without an
  account.

`sbx` adds the microVM, built-in credential injection, network policy presets,
clone mode, cloud sandboxes and organisation governance. The two are better
used side by side than one inside the other.

## Maintenance

- A second backend about the size of `core/docker.py` plus the launch paths in
  `commands/run.py` and `commands/task.py`, built on a weekly-changing pre-1.0
  CLI with no stable local API.
- A second image variant per agent to meet the `sbx` user/launch contract, kept
  in sync with the `vibepod-agents` entrypoint.
- Tests could only mock the CLI. A real integration test needs KVM (or macOS or
  Windows hosts), a signed-in Docker account and network access, which CI does
  not have.

## When to revisit

Reopen the task if any of these change:

- Docker Sandboxes reaches 1.0 with a stable, documented local API (not only
  the CLI);
- `sbx` lets a sandbox use an arbitrary OCI image with its own entrypoint and
  user, or VibePod's images move to a non-root `agent` UID 1000 layout;
- `sbx` supports mount targets (or VibePod drops the fixed `/workspace` and
  config paths) and lets a per-sandbox upstream proxy keep per-launch identity;
- local sandboxes no longer require a Docker sign-in, or VibePod users ask for
  `sbx` specifically.

Until then, use the Kata sandbox for VM-level isolation, the gVisor sandbox
(VP-144) where KVM is not available, and `sbx` directly when you want Docker
Sandboxes' credential isolation and network policy.
