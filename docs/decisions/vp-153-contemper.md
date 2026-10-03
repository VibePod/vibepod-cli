# VP-153: Contemper for agent VMs

Date: 2026-10-03. Decision: **no-go for a VibePod backend now**.
Task disposition: **closed with this analysis; no prototype**. Contemper remains
a candidate image authoring tool for the VM sandbox work in VP-143, not a
sandbox runtime. Docker/Podman behavior and defaults remain unchanged.

## Image changes and cost

A separate VM variant in `vibepod-agents` would need a kernel at
`/boot/contemper/vmlinuz`, a generic initrd at `/boot/contemper/initrd`,
`/sbin/init`, and `LABEL io.contemper.ready="true"`. Add an appropriate
serial console in `/boot/contemper/cmdline`, root filesystem configuration,
virtio storage/network drivers, and guest networking. For a Debian variant,
upstream demonstrates `linux-image-${TARGETARCH}`, `systemd-sysv`, `udev`,
`initramfs-tools`, and `e2fsprogs`.

Contemper ignores OCI `ENTRYPOINT`, `CMD`, `ENV`, `USER`, and `WORKDIR`.
A guest service must recreate the agent entrypoint contract: launch arguments,
environment, working directory, runtime user, initialization and exit status.
Installing boot packages alone will not start the agent. See upstream's
[authoring contract](https://contemper-project.github.io/contemper/guide/authoring/)
and [Debian example](https://github.com/contemper-project/contemper/blob/68dfbf1ca763fd269ac5468a2d5fa53600cad379/examples/debian/Containerfile).

Cost is an additional kernel/modules tree, initrd, init system and guest
services, plus OCI build artifacts and converted disks. Shared VM base layers
could amortize downloads across agents, but each agent/architecture combination
still needs boot validation, kernel security updates and coordinated releases.
No image was built for this analysis, so there is no measured size or boot-time
claim. Before adoption, measure compressed OCI delta, unpacked filesystem size,
allocated qcow2 size and conversion peak disk use against the same agent digest.
Sparse disk capacity is not a download-size measurement.

## VibePod feature compatibility

These are proposed integration requirements, not supported VibePod features.
Current contracts live in `src/vibepod/core/docker.py` (`run_agent`,
`ensure_proxy`, `attach_interactive`) and `src/vibepod/commands/run.py`.

| Feature | What a VM backend would require |
| --- | --- |
| `/workspace`, extra mounts, overlays and skills | Contemper `VOLUME` creates persistent guest block disks, not live host binds. Use a separately managed virtiofs/9p share or explicit copy/sync, preserving ownership, permissions and visibility of edits to the host editor. ACP also needs its requested workspace path. |
| Credentials and agent config | Mount or inject per-session config at the paths in `AGENT_SPECS`, with a matching guest UID and controlled write-back. Do not bake credentials into OCI layers or reusable bundles. Keep authentication/resume state deliberately persistent and isolate instances. |
| Proxy and provider settings | The guest cannot resolve Docker-network service names automatically. Provide a host-reachable proxy endpoint, guest DNS/routes, proxy environment and CA trust. The current proxy is not published to the host by `ensure_proxy`; publishing an endpoint needs authentication/exposure review. NAT alone does not enforce proxy-only egress. |
| Published ports and OAuth callbacks | Explicit QEMU `hostfwd` mappings or TAP/bridge forwarding, including loopback binding, collision handling and callback addressing. `EXPOSE` is metadata, not an implemented port mapping. |
| Interactive agent and `vp attach` | A persistent guest PTY/session service and host transport (for example SSH plus a guest session manager, or a vsock protocol), including input, resize, detach, reconnect and exit status. Boot logs are insufficient. |
| List, stop, cleanup and tasks | Backend-neutral session IDs, process supervision, readiness, cancellation and instance disk cleanup. Separate ephemeral root writes from persistent workspace/config state. |
| ACP and host sockets | A dedicated byte-clean stdin/stdout channel separate from boot logs; SSH/Herdr sockets need an explicit relay and access policy. A host Unix socket inode cannot simply be mounted across the VM boundary. |

Upstream's [volume guide](https://contemper-project.github.io/contemper/guide/volumes/)
documents sized ext4 disks, instance persistence and refusal to resize existing
disks. Local root writes are discarded on redeploy. More significantly,
[`qemu.go` at the inspected commit](https://github.com/contemper-project/contemper/blob/68dfbf1ca763fd269ac5468a2d5fa53600cad379/internal/qemu/qemu.go)
sets `snapshot=on`, `-netdev user,id=net0` with no `hostfwd`, and
`-serial file:<log>`. Deploy tails that log; it does not provide an input-capable
serial transport. Despite the documentation's serial-login example, the current
implementation is not a usable interactive agent session. No host filesystem
share or guest launch/configuration API is provided there.

## VMM options and Kata

| Route | Fit of the current Contemper bundle |
| --- | --- |
| QEMU | Supported conversion/deploy route: UEFI boots a UKI from the EFI partition, with writable ext4 root in qcow2. VibePod still needs the runtime integrations above. |
| Cloud Hypervisor (VP-147) | UEFI is supported with Cloud Hypervisor's EDK II firmware, so reuse of the disk layout is plausible. A conservative route is qcow2-to-raw conversion plus that firmware, compatible guest drivers and an independent launcher. This is untested here and is not a Contemper target. See [UEFI boot](https://github.com/cloud-hypervisor/cloud-hypervisor/blob/main/docs/uefi.md). |
| Firecracker (VP-146) | Its documented route loads a separate Linux kernel and ext4 rootfs, rather than booting a UEFI qcow2 bundle. It needs kernel/rootfs extraction or different output packaging, appropriate kernel configuration and its own networking/session runtime. Converting qcow2 to raw alone does not supply the kernel or turn a partitioned disk into the documented rootfs. See [getting started](https://github.com/firecracker-microvm/firecracker/blob/main/docs/getting-started.md). |
| Incus | Planned upstream; not an available integration on which to base this task. |

Kata (VP-145) runs the existing OCI workload inside its own managed guest and
supplies a guest agent and container lifecycle transport. It therefore avoids
per-agent kernel/init variants and preserves OCI launch semantics. It still
requires a supported host/runtime setup and validation of VibePod mounts,
networking and attach behavior; its containerd shim integration is not a
drop-in change to this CLI's Docker SDK. Prefer evaluating Kata first for VM
isolation with unchanged agent images. See the
[Kata architecture](https://github.com/kata-containers/kata-containers/blob/main/docs/design/architecture/README.md).

## Maturity, requirements and decision

The inspected upstream revision is
[`68dfbf1ca763fd269ac5468a2d5fa53600cad379`](https://github.com/contemper-project/contemper/commit/68dfbf1ca763fd269ac5468a2d5fa53600cad379).
The [changelog](https://github.com/contemper-project/contemper/blob/68dfbf1ca763fd269ac5468a2d5fa53600cad379/CHANGELOG.md)
records first release 0.1.0 on September 28 and 0.2.0 on September 29, 2026.
The project is Go/Apache-2.0, with LGPL systemd-stub assets. Its
[status](https://contemper-project.github.io/contemper/) explicitly allows
pre-1.0 CLI/bundle changes; the manifest currently has `formatVersion: 1`.
Upstream boot CI is useful evidence of bootability, not evidence of VibePod
feature parity or sandbox policy.

A future QEMU experiment needs Contemper, `e2fsprogs`, `qemu-img`, an
architecture-matching `qemu-system-*` and UEFI firmware. Linux KVM requires
read/write `/dev/kvm`; macOS uses HVF; foreign architecture or unavailable
acceleration falls back to slower TCG. Docker/buildx is required for
`contemper build`, while conversion can consume an independently built OCI
archive. These are upstream requirements, not a newly supported VibePod mode.
See [host tools](https://contemper-project.github.io/contemper/reference/host-tools/).

**No-go now:** conversion solves only image packaging. An integration would
also introduce a VM image release matrix and a substantial guest/host runtime,
while neither interactive attach nor live workspace/proxy/port compatibility
comes from the current local deploy implementation. The young, explicitly
unstable bundle interface adds maintenance risk to that work. VP-143's sandbox
selection/backend abstraction is also absent from this checkout.

Revisit after VP-143 defines the backend contract, a stable or explicitly
pinned/version-checked bundle interface is available, and an isolated upstream
experiment demonstrates bidirectional TTY, live workspace sharing and proxy
connectivity with measured image cost. Stability alone will not remove the
runtime gaps. Any subsequent opt-in prototype must keep defaults unchanged,
document platform limits, and skip VM integration tests when QEMU/firmware are
unavailable. This task closes on the no-go analysis; no VM backend or QEMU test
is added.
