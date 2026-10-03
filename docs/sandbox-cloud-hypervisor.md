# Cloud Hypervisor: analysis (no separate sandbox)

VibePod does **not** add Cloud Hypervisor as a sandbox of its own. This page
explains why and records the decision on VP-147, so the question can be
reopened if the constraints below change.

**Decision: no-go for a separate Cloud Hypervisor sandbox or a direct
backend.** Cloud Hypervisor is worth using, but only as the *hypervisor of
the Kata sandbox* (VP-145). With Kata, the hypervisor is picked by which Kata
runtime the engine has registered (`kata-clh` versus `kata-qemu`), not by
anything VibePod passes. Choosing the runtime name is all the "Cloud
Hypervisor option" needs, so it belongs in the Kata integration, not in a
second sandbox mode. A direct integration would bring nothing over the Kata
path and would need the same second backend that the
[Firecracker analysis](https://vibepod.dev/docs/sandbox-firecracker/) rejected.

Recommendation for the Kata sandbox (VP-145): make the runtime name
configurable (e.g. `sandbox.kata.runtime`), document `kata-clh` as the
preferred setup, and keep `kata-qemu` as the fallback for hosts or devices
that Cloud Hypervisor does not support.

## What Cloud Hypervisor is

[Cloud Hypervisor](https://github.com/cloud-hypervisor/cloud-hypervisor) is a
virtual machine monitor (VMM) written in Rust. It runs on KVM (Linux) and
MSHV (Microsoft hypervisor), on x86_64 and aarch64. Its device model is small
but complete enough for containers: virtio-block, virtio-net, virtio-vsock,
**virtio-fs**, virtio-pmem, VFIO, and CPU/memory/device hotplug. Like
Firecracker it boots a kernel and a disk image; it does not run OCI images
itself. Kata Containers ships it as one of its hypervisors
(`configuration-clh.toml`).

## The two possible shapes

### 1. Through Kata (`kata-clh`) — the recommended path

Docker runs Kata through the containerd shim (`containerd-shim-kata-v2`).
Kata picks its hypervisor from the configuration file the shim loads, so a
Cloud Hypervisor runtime is registered like this:

```json
{
  "runtimes": {
    "kata-clh": { "runtimeType": "/usr/local/bin/containerd-shim-kata-clh-v2" }
  }
}
```

Here `containerd-shim-kata-clh-v2` is a small wrapper that sets
`KATA_CONF_FILE=/opt/kata/share/defaults/kata-containers/configuration-clh.toml`
and runs `containerd-shim-kata-v2`. (kata-deploy and the Kata static tarball
install the same handler names for containerd.) VibePod only passes
`--runtime kata-clh`. Everything else is the same for any Kata hypervisor:
images, mounts, the proxy sidecar, `vibepod-network`, published ports and the
TTY. The Kata sandbox's prerequisite checks and docs therefore cover Cloud
Hypervisor too.

Unlike Firecracker, Cloud Hypervisor has **virtio-fs**. Kata shares the
image's root filesystem and host bind mounts into the guest through
`virtiofsd`. The live read-write `/workspace` mount, the agent config
directory, the proxy CA certificate, and skills/overlay mounts keep working.
The Firecracker analysis ruled Firecracker out for exactly this reason.

### 2. A separate VibePod backend driving Cloud Hypervisor directly

This would remove Kata's guest agent and shim, but VibePod would have to
rebuild them:

| Piece | Work required |
| --- | --- |
| Image → disk | Flatten each agent image (and every overlay/profile variant) into a disk image, cache it, rebuild it when the digest changes, and supply a guest kernel. Kata ships both. |
| Guest init | Mount virtio-fs shares, set up the user, environment and entrypoint. `kata-agent` does this today. |
| `/workspace` | Run and supervise one `virtiofsd` per share. This is workable (unlike Firecracker), but it is what Kata already does. |
| Networking | TAP device, bridge/NAT or routing to the proxy sidecar, DNS by name. This needs `CAP_NET_ADMIN` and replaces `vibepod-network`. |
| Console / lifecycle | Serial or vsock PTY for `vp run`/`vp attach`/ACP stdio, plus `vp list`, `vp stop`, labels, task containers and resume. |

That is a second container engine, Linux/KVM-only, and it needs root for
networking. It isolates no better than Kata with Cloud Hypervisor, which uses
the same VMM. **A direct integration brings nothing VibePod needs.**

## Kata + Cloud Hypervisor vs Kata + QEMU vs Firecracker

| | Kata + Cloud Hypervisor | Kata + QEMU | Firecracker (Kata or direct) |
| --- | --- | --- | --- |
| Startup (container ready) | ~0.5–1 s | ~1–2 s | ~1 s through Kata (VMM boot ~125 ms) |
| VMM memory overhead | low (tens of MiB) | higher (~100+ MiB, larger device model) | lowest (< 5 MiB) |
| Guest memory | Kata default 2 GiB; hotplug supported | same; hotplug supported | sized at creation, no hotplug |
| `/workspace` sharing | virtio-fs (DAX optional) | virtio-fs (9p in old configs) | none: block device only |
| Docker storage | default (overlayfs) | default (overlayfs) | devmapper snapshotter required |
| Device support | virtio, VFIO; no legacy devices | widest (legacy, more archs, GPU paths) | minimal |
| VMM attack surface | small, Rust | large, C | smallest, Rust + jailer |
| Fit for VibePod | **best Kata default** | fallback | no-go (VP-146) |

The startup and memory figures are typical values from Kata's defaults, not
VibePod measurements. Agent CLIs take several seconds to start anyway, and
Node.js agents need about as much memory as Kata's default guest has, so the
difference between Cloud Hypervisor and QEMU is small next to the agent
itself. The I/O path for `/workspace` (virtio-fs through `virtiofsd`) is the
same for both. Metadata-heavy work (`git status`, `npm install` in large
trees) is noticeably slower than a native bind mount on either hypervisor.

## Host requirements and platforms

| Host | Kata + Cloud Hypervisor available? |
| --- | --- |
| Linux, bare metal, `/dev/kvm` accessible, rootful Docker | Yes (x86_64, aarch64) |
| Linux VM / cloud instance | Only with nested virtualisation enabled or on metal instances |
| macOS (Docker Desktop, Podman machine, Colima, OrbStack) | No: no KVM inside the Linux VM |
| Windows (Docker Desktop / WSL2) | Not in practice (nested KVM in WSL2 is experimental, and Docker Desktop's VM does not ship Kata) |
| Rootless Docker / rootless Podman | No: the shim, `virtiofsd` and the VM networking need root |
| Podman (rootful) | Not supported upstream: Kata 2+/3 targets containerd and CRI-O through shim v2, which Podman does not use |
| GitHub-hosted Linux runners | `/dev/kvm` is exposed on standard Ubuntu runners, so a CI job could install Kata to run integration tests |

Most VibePod users run Docker Desktop or rootless Podman, so VM isolation of
any kind (Kata with any hypervisor) stays an opt-in for Linux hosts with
rootful Docker and KVM. Where KVM is missing, the gVisor sandbox (VP-144)
remains the option.

## Isolation gained

Kata with Cloud Hypervisor runs the agent on its own guest kernel behind
KVM, so a kernel exploit or container escape stays inside the guest. Compared
with QEMU, the boundary is the same, but the VMM that an escaped guest would
attack next is much smaller and memory-safe. That matters only against VMM
exploits, which is why it is a good *default* and not a reason for a separate
mode.

As with gVisor and Firecracker, VM isolation protects the **host kernel**. It
does not limit what the agent can do with what VibePod gives it: the
workspace and agent config directory are still mounted read-write,
credentials in the environment are still readable, and network egress still
goes through the proxy, which observes and filters it and is not a sandbox.

## Compatibility with VibePod features (Kata + Cloud Hypervisor)

| Feature | Status |
| --- | --- |
| Live `/workspace` bind mount | Yes (virtio-fs) |
| Agent config persistence / resume | Yes (virtio-fs) |
| Proxy sidecar, CA cert mount, `vibepod-network` DNS | Yes (Kata bridges the container's network namespace into the guest) |
| Skills / overlays / profiles mounts | Yes |
| herdr socket | No: unix sockets do not cross virtio-fs into the guest, so it must be skipped as in the gVisor sandbox |
| Rootless engines, Docker Desktop, Podman | No |
| `vp attach`, `vp logs`, ACP stdio | Yes (through the engine) |

These are the same as for Kata + QEMU. Both must be handled once, in the Kata
sandbox.

## Maintenance

- **Through Kata:** nothing beyond the Kata sandbox. Kata pins and tests the
  Cloud Hypervisor version it ships. VibePod only needs to know the runtime
  name and document `kata-clh` next to `kata-qemu`.
- **Direct backend:** a second container engine, a guest image pipeline, and
  tracking Cloud Hypervisor's REST API and release cadence. That cost buys
  nothing over the Kata path.

## When to revisit

Reopen the task if any of these change:

- the Kata sandbox (VP-145) shows that a per-hypervisor setting is needed
  beyond the runtime name, for example a check that differs between `kata-clh`
  and `kata-qemu`;
- Docker Desktop, Podman machine or WSL2 start shipping Kata with nested
  KVM, which would widen the platforms where this matters;
- VibePod adds a workflow without a container engine, where driving a VMM
  directly would be the only option.

Until then, use the Kata sandbox with the `kata-clh` runtime for VM-level
isolation, and the gVisor sandbox where KVM is not available.
