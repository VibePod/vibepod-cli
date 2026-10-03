# Bubblewrap evaluation (VP-149)

**Decision: no-go for a supported VibePod process sandbox.** Both wrapping
agents inside containers and launching them directly on the host were evaluated.
VP-149 concludes with this decision; no sandbox setting or launch flag is added,
and default behavior stays unchanged. This is a supportability decision, not a
claim that bubblewrap cannot sandbox a coding agent.

## Platforms and prerequisites

Bubblewrap is Linux-only. Its current upstream implementation requires user
namespaces rather than a setuid fallback. Package installation alone is therefore
insufficient: namespace creation and mount operations must also be permitted by
the kernel and the runtime security policies. See the
[upstream overview](https://github.com/containers/bubblewrap).

| Launch location | Assessment |
| --- | --- |
| Linux Docker container | Default seccomp blocks namespace/mount operations needed for setup; AppArmor can independently deny mounts. A supported integration would need reviewed custom policies. |
| Linux Podman container | Nested user namespaces depend on the host, rootless UID mapping, seccomp and AppArmor/SELinux policy. Rootless operation alone does not establish compatibility. |
| Docker Desktop / Podman machine | An inner sandbox would run in the Linux VM and inherit its restrictions. A successful native Linux test would not validate these environments. |
| Native Linux host | Potentially viable with permitted unprivileged user namespaces, but requires a separate agent launcher and policy. |
| Native macOS / Windows host | Unsupported by bubblewrap; no native host mode. |

[Docker seccomp documentation](https://docs.docker.com/engine/security/seccomp/)
describes the blocked calls, including `mount`, `pivot_root`, and namespace
creation. The [Moby AppArmor template](https://github.com/moby/profiles/blob/main/apparmor/template.go)
also denies mounts. [Podman security options](https://docs.podman.io/en/latest/markdown/podman-run.1.html#security-opt-option)
describe its independent confinement controls. Turning off these controls or
adding `--privileged` is not an acceptable default prerequisite for an extra
sandbox. Narrow policy exceptions may be feasible, but require testing and
ongoing review across supported runtimes.

## Policy and agent compatibility

A candidate container policy would expose the image read-only, keep `/workspace`
and the agent's persisted config mount writable, provide private temporary
storage and `/proc`, and isolate PID/IPC namespaces. This would reduce writes to
the container filesystem and access to sibling processes. It would still share
the kernel and permit modification of the workspace and agent credentials.
Read-only files remain readable: exposing the entire root read-only would not
protect secrets in other mounted directories. Every additional volume would
need an explicit visibility and write policy.

The authoritative per-agent paths and launch constraints are in
`src/vibepod/core/agents.py`. There is no demonstrated common policy for all
agents:

- Credentials, sessions and caches within the persisted config mount can remain
  writable; caches or user tool installs outside it would fail or require
  additional writable paths. System package installation into a read-only image
  would fail. Project overlays are a better place for those dependencies.
- Git writes inside the workspace should remain possible. Worktrees can refer
  to Git metadata outside that mount; signing, SSH and credential helpers can
  require additional executables, files or sockets. A workspace bind alone does
  not establish that those operations work.
- Hermes requires its image's runtime user and startup preparation. Sandboxing
  the native entrypoint can prevent preparation; bypassing it loses that setup.
  Config mounts also differ between agents, so `/config` cannot be hardcoded.
- Init commands, provider wrappers and ACP adapters create more launch paths.
  A wrapper must define whether initialization is trusted and outside the
  sandbox, then ensure every actual agent invocation enters it.

These are predicted consequences of the policy and existing launch contracts,
not results from running each agent. Bubblewrap's PID isolation supplies no
CPU, memory or process-count quota; resource limits need separate runtime or
cgroup controls.

## VibePod features

| Feature | Required handling |
| --- | --- |
| Proxy and model providers | Retain network access, proxy environment and read-only CA mounts. A private network namespace has no route to the proxy or a remote model API, so no-network task runs require an independently usable offline model path. Keeping networking does not enforce proxy use. |
| TTY, attach and resume | Preserve streams, exit codes, signal handling and interactive terminal behavior. A later engine exec does not automatically join a process sandbox's namespaces. |
| herdr hooks | Expose the intended Unix socket and hook scripts/binary. Socket access permits communication with the host service even under filesystem restrictions. A private `/tmp` can hide existing socket mounts unless they are explicitly restored. |
| ACP | Preserve host-path workspace aliases and JSON-RPC-only stdout, plus stdin EOF lifecycle handling. |
| Published ports / web agents | Network isolation would sever listeners from container port forwarding. |
| Task stop, timeout and logs | Ensure descendants terminate, and preserve the container lifecycle and task registry contract. |

The [bubblewrap manual](https://github.com/containers/bubblewrap/blob/main/bwrap.xml)
documents that `--new-session` disconnects the controlling terminal; omitting it
requires addressing terminal input injection. It also documents parent-death
handling. An interactive wrapper therefore needs an explicit terminal policy
and real attach/signal tests, rather than only command-construction tests.

A host backend would have to replace container-based discovery, task stop/logs,
image provisioning, path mapping and proxy reachability. It could provide a
useful filesystem/process boundary, but would not inherit VibePod's image,
runtime security policy or container lifecycle. It should be designed as a
separate Linux backend with its own threat model, not offered as an automatic
fallback when Docker/Podman is missing.

## Evidence, overhead and maintenance

During this evaluation, the Linux development environment had neither `bwrap`
nor a Docker/Podman executable or standard runtime socket. The namespace sysctls
were permissive (`user.max_user_namespaces=11884` and
`kernel.unprivileged_userns_clone=1`), but the following prerequisite probe failed:

```console
$ unshare --user --map-root-user --mount --pid --fork true
unshare: cannot change root filesystem propagation: Permission denied
```

This demonstrates that sysctl checks alone would not be an adequate preflight;
it does not identify which policy denied the operation or prove failure on
every supported host. No per-agent compatibility or performance benchmark was
possible here. Source review establishes the default-policy conflicts above.

Expected overhead is namespace/mount setup at launch and a small supervisor
process, with no guest OS or emulation. No latency or memory figures are claimed.
The main maintenance cost is the policy matrix: image entrypoints, writable
paths, UID mappings, terminals, runtime security policies and host distributions.
An additional wrapper is not enough to validate that matrix.

## Conditions for reconsideration

Revisit after VP-143 provides a sandbox/backend contract and an image entrypoint
hook that runs after required preparation. A go decision should include:

1. A specified threat model and reviewed runtime policy exceptions that retain
   outer confinement, rather than requiring privileged/unconfined containers.
2. Successful prerequisite probes under the actual image, runtime user and
   security policy, with actionable errors and no silent unsandboxed fallback.
3. Real agent trials for credentials, caches, tool installs and Git, plus
   proxy, herdr, ACP, TTY/attach, exit status and descendant cleanup tests.
4. A documented runtime/platform matrix and measured launch overhead. Tests
   requiring unavailable runtimes must skip with an explicit reason.

For now, continue using VibePod's container launch path. No bubblewrap setup is
required or supported by the CLI. Agent-provided internal sandboxing is a
separate concern; installing bubblewrap in an agent image does not implement a
VibePod-wide policy.
