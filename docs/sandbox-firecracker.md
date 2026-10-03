# Firecracker microVMs: analysis (no-go)

VibePod does **not** offer a Firecracker sandbox. This page explains why. It
records the decision on VP-146, so the question can be reopened if the
constraints below change.

**Decision: no-go.** Neither integration shape can keep VibePod's live
read-write `/workspace` bind mount. It also could not run on most developer
machines (macOS, Windows, Docker Desktop, rootless Podman). Kata Containers
with Cloud Hypervisor or QEMU gives the same hardware-virtualisation boundary
without these problems, so VM isolation should come from the Kata sandbox
(VP-145).

## What Firecracker is

[Firecracker](https://github.com/firecracker-microvm/firecracker) is a
KVM-based virtual machine monitor (VMM). It boots minimal microVMs with a
small device model: virtio-net, virtio-block, virtio-vsock, a serial console
and a balloon device. AWS built it for Lambda and Fargate. It does not run OCI
images; it boots a kernel and a root filesystem image. Running containers on
it needs another layer: Kata Containers (the `kata-fc` runtime) or
firecracker-containerd.

## The two possible shapes

### 1. Through Kata (`io.containerd.kata-fc.v2`)

Kata can use Firecracker as its hypervisor, but with tighter limits than its
other hypervisors:

- **Rootfs must be a block device.** Firecracker has no virtio-fs and no 9p,
  so Kata cannot share the image's overlay filesystem into the guest. The
  [Kata Firecracker guide](https://github.com/kata-containers/kata-containers/blob/main/docs/how-to/how-to-use-kata-containers-with-firecracker.md)
  requires containerd's **devmapper snapshotter**: a thin pool on loop
  devices, a containerd config change, and images pulled with
  `--snapshotter devmapper`. Docker's default storage (overlay2, or the
  overlayfs snapshotter) cannot back a `kata-fc` container. Podman has no
  devmapper snapshotter at all.
- **No host directory sharing.** Without virtio-fs, Kata cannot pass a host
  bind mount into the guest as a live shared filesystem. At most it copies
  small files in when the container is created, and nothing is written back.
  VibePod depends on live, two-way bind mounts:
  - `/workspace` (the agent edits the project and the user sees the changes),
  - the agent config directory (logins, sessions, resume),
  - the proxy CA certificate,
  - skills and overlay mounts,
  - the herdr socket.

  Without them, the agent would edit a throwaway copy of the project.
- **Engine support.** Kata's documented Firecracker path is `ctr` and
  Kubernetes (CRI). Using `docker run --runtime io.containerd.kata-fc.v2` also
  needs Docker's containerd image store set to the devmapper snapshotter.
  That setup changes the whole Docker daemon, not only VibePod, and Kata does
  not support or test it.

So the Kata shape is technically reachable only on a rootful Linux Docker host
with a reconfigured daemon, and even there the main VibePod workflow
(working on a host project) does not work.

### 2. A separate VibePod backend driving Firecracker directly

VibePod would have to replace the container engine for the agent. That means:

| Piece | Work required |
| --- | --- |
| Image → rootfs | Flatten each agent image (and every overlay/profile variant) into an ext4 image, cache it, rebuild it when the image digest changes, and supply a guest kernel. |
| Init / entrypoint | Ship a guest init that sets up the user, environment, mounts and the agent entrypoint that the container runtime provides today. |
| Networking | Create a TAP device per VM, plus a bridge/NAT or routing to the proxy sidecar. This needs `CAP_NET_ADMIN` (root) on the host and replaces `vibepod-network`, DNS-by-name to the proxy, and published ports. |
| `/workspace` | No virtio-fs. Options: a block device (not shareable live with the host), NFS/9p over the network (slow, more host daemons), or rsync/vsock sync (not live, risks conflicts). None of them matches a bind mount. |
| Console / TTY | Wire the serial console or a vsock PTY to `vp run`'s interactive terminal, `vp attach`, `vp logs`, resize signals and ACP stdio. |
| Lifecycle | Re-implement `vp list`, `vp stop`, labels, task containers and resume on top of Firecracker processes and the jailer. |

This is a second runtime backend roughly the size of `core/docker.py` plus
`commands/run.py` together, Linux/KVM only, and needs root for networking.
The analysis does not justify it.

## Platforms

| Host | Firecracker available? |
| --- | --- |
| Linux, bare metal, `/dev/kvm` accessible | Yes (x86_64, aarch64) |
| Linux VM / cloud instance | Only with nested virtualisation or metal instances |
| macOS (Docker Desktop, Podman machine, Colima) | No (no KVM; nested KVM inside the Linux VM is not generally available) |
| Windows (Docker Desktop / WSL2) | Not supported in practice (nested KVM in WSL2 is experimental) |
| GitHub-hosted CI runners | Linux runners expose `/dev/kvm` on larger/standard Ubuntu images, but the devmapper/containerd setup would have to be built per job |
| Rootless Docker / Podman | No (devmapper snapshotter, TAP networking and the jailer need root) |

Most VibePod users run Docker Desktop or rootless Podman, so a Firecracker
sandbox would be unavailable to them.

## Overhead

- **Startup:** a bare Firecracker microVM boots in roughly 125 ms. Through
  Kata, the guest agent, devmapper snapshot activation and container setup
  push this to around one second. Agent CLIs take several seconds to start
  anyway, so Firecracker's boot speed is not noticeable to VibePod users.
- **Footprint:** under 5 MiB of VMM memory per microVM, plus guest kernel and
  guest memory (set by Kata, e.g. 2 GiB by default). Memory is not
  hot-pluggable with Firecracker, so the VM is sized when it is created.
  Node.js-based agents need about as much memory as Kata+Cloud Hypervisor
  gives them.
- **Disk:** the devmapper thin pool and a full block snapshot of each image
  replace overlay layer sharing.

## Isolation gained

Firecracker gives the same kind of boundary as Kata with Cloud Hypervisor or
QEMU: the agent runs on its own guest kernel behind KVM, so a container
escape or kernel exploit stays inside the guest. Firecracker adds:

- a smaller VMM attack surface (minimal device model, written in Rust; Cloud
  Hypervisor is also Rust and similarly minimal),
- the `jailer` (chroot, namespaces, seccomp for the VMM process).

The [gVisor sandbox](https://vibepod.dev/docs/sandbox/) notes also apply here:
VM isolation protects the **host kernel**. It does not limit what the agent
can do with what VibePod gives it (the workspace, credentials in the
environment, and network egress through the proxy). For VibePod's threat
model, Kata+Cloud Hypervisor and Firecracker are equivalent. The smaller
device model only matters against VMM exploits, and Cloud Hypervisor already
covers most of that difference.

## Compatibility with VibePod features

| Feature | Kata + Firecracker | Direct backend |
| --- | --- | --- |
| Live `/workspace` bind mount | No | No (sync only) |
| Agent config persistence / resume | No | Needs custom sync |
| Proxy sidecar, CA cert, `vibepod-network` DNS | Network yes, CA mount no | Custom TAP + routing |
| Skills / overlays / profiles mounts | No | Rebuild into rootfs |
| herdr socket | No | No |
| Rootless Podman / Docker Desktop | No | No |
| `vp attach`, `vp logs`, ACP stdio | Yes (via engine) | Re-implement |

## Maintenance

- Kata's Firecracker support gets less attention than QEMU and Cloud
  Hypervisor: fewer features (no virtio-fs, no hotplug) and a narrower tested
  path (containerd + devmapper).
- VibePod would have to document and support host-wide containerd/Docker
  storage changes it cannot check or undo safely.
- A direct backend would mean maintaining a second container engine.

## When to revisit

Reopen the task if any of these change:

- Firecracker gains a virtio-fs (or equivalent live shared filesystem) device
  and Kata enables host bind mounts for `kata-fc`;
- Docker or Podman support `kata-fc` without a host-wide devmapper
  reconfiguration;
- VibePod adds a workflow without a host workspace (e.g. agents that clone the
  repository inside the sandbox and push results), where the bind-mount
  limitation would not matter.

Until then, use the Kata sandbox (Cloud Hypervisor or QEMU) for VM-level
isolation, and the gVisor sandbox where KVM is not available.
