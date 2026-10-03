# VP-151: Hyperlight for sandboxing agent work

Decision: **no-go**. Analysis completed on 2026-10-03; close VP-151 with no
runtime implementation. No current VibePod operation justifies the integration
and maintenance cost. This document is the repository's task decision record.

## Guest model and host platforms

[Hyperlight](https://github.com/hyperlight-dev/hyperlight#readme) is a CNCF
sandbox project and an embedded Rust VMM library. Its core guests are ELF
binaries built against its guest library in `no_std` Rust or C. They have no
kernel or OS, and communicate with the host through typed function calls.
Filesystem and network capabilities require explicit host functions. Low
startup latency does not supply Linux process or syscall compatibility: core
Hyperlight cannot directly run an agent CLI, shell, Node.js or Python.
The core API is pre-1.0 and may change between releases.

The upstream [setup guide](https://github.com/hyperlight-dev/hyperlight/blob/main/docs/getting-started.md)
documents Linux with KVM (`/dev/kvm`) or Microsoft Hypervisor (MSHV,
`/dev/mshv`), and Windows with Windows Hypervisor Platform (WHP). The process
needs access to the hypervisor; building native guests also needs a Rust and
Clang/LLVM toolchain. That guide does not document a native macOS core backend.
Access to a Docker/Podman socket, including a remote engine, does not establish
that the CLI host has an accessible hypervisor. A helper inside a VM or
container would need device access and, where applicable, nested virtualization.

## Wasm and related projects

[Hyperlight-Wasm](https://github.com/hyperlight-dev/hyperlight-wasm#readme)
does provide the narrower Wasm path: a Wasm runtime runs inside a Hyperlight
guest, with user modules or components loaded through a Rust host API. Its
[SDK guide](https://github.com/hyperlight-dev/hyperlight-wasm/blob/main/RustDev.md)
describes the runtime guest and explicit host-function registration. It uses
the same Linux KVM/MSHV and Windows WHP backends. Upstream labels it experimental
and not production-grade. A Wasm function is feasible; existing agent CLIs and
their workspace tooling do not become Wasm workloads automatically.

The task description's limitation applies to **core Hyperlight**. The separate
[Hyperlight-Unikraft](https://github.com/hyperlight-dev/hyperlight-unikraft#readme)
now advertises ordinary Linux programs, including Python, Node.js and Bash,
using a Unikraft guest kernel, a rootfs and opt-in filesystem/network access.
It also advertises Apple silicon macOS support. This is a different runtime
model, not evidence that core OS-less guests can run agents. VibePod compatibility
has not been tested: a replacement would need validation of agent subprocesses,
interactive terminals, workspace binds, credentials, proxy networking and ACP,
plus a new lifecycle/image backend alongside Docker/Podman.

## Concrete VibePod candidates

These are code-review findings and engineering estimates, not benchmarks or
claims that the existing parsers are immune to malicious input.

| Operation and current boundary | Potential benefit | Effort and assessment |
| --- | --- | --- |
| Agent output resume hints: [`resume.py`](https://github.com/VibePod/vibepod-cli/blob/7c6a959/src/vibepod/core/resume.py), fed a bounded 64 KiB tail by [`docker.py`](https://github.com/VibePod/vibepod-cli/blob/7c6a959/src/vibepod/core/docker.py) | Isolate regex/string processing of agent-controlled output | Port patterns and formatting to a native or Wasm guest and bridge results. It only constructs a displayed command; no agent-supplied code is executed. Low benefit for a substantial new runtime dependency. |
| ACP pre-launch JSON frames: [`acp.py`](https://github.com/VibePod/vibepod-cli/blob/7c6a959/src/vibepod/core/acp.py); skills-engine JSON results: [`skills_engine.py`](https://github.com/VibePod/vibepod-cli/blob/7c6a959/src/vibepod/core/skills_engine.py) | Contain parser faults or resource exhaustion | Replace Python JSON parsing, preserve types/errors and byte replay, enforce input/output/time/memory budgets. Host framing and result validation still need limits; sandboxing only the parser leaves those allocations outside the VM. No measured need for this complexity. |
| Imported provider TOML: [`provider_exchange.py`](https://github.com/VibePod/vibepod-cli/blob/7c6a959/src/vibepod/core/provider_exchange.py), [`providers.py`](https://github.com/VibePod/vibepod-cli/blob/7c6a959/src/vibepod/core/providers.py); project/proxy YAML: [`config.py`](https://github.com/VibePod/vibepod-cli/blob/7c6a959/src/vibepod/core/config.py), [`proxy_filter.py`](https://github.com/VibePod/vibepod-cli/blob/7c6a959/src/vibepod/core/proxy_filter.py) | Isolate parsing of externally supplied data | Port TOML/YAML semantics and return a constrained schema. Provider exchanges already have a 256 KiB fetch limit; YAML uses safe loading. Host validation and filesystem writes remain trusted responsibilities. Insufficient benefit to duplicate parsers. |
| Skill acquisition/transformation in the skills-engine container | Stronger isolation of third-party content processing | The CLI already delegates execution to a separate image. Replacing its filesystem/network workflow with purpose-built guest functions requires changes in that engine as well as this repository. Not a small CLI integration. |

Even one pure parser would require a Python-to-Rust extension or helper protocol,
guest build/distribution, capability checks, bounded calls and crash handling,
platform CI, and an opt-in configuration/docs contract. Budget several engineer
weeks for a supported first integration, with ongoing upstream API maintenance;
this is an estimate, not a prototype result. Wasm reduces guest portability work
but does not remove the bridge, hypervisor or packaging costs.

## Task disposition

**No-go; VP-151 is ready to close as an analysis task.** Keep the existing
Docker/Podman runtime. Do not add a setting or dependency without a selected
workload. Revisit if VibePod gains untrusted user-supplied Wasm transforms or
code-execution plugins with a small capability interface, or if a separate
Unikraft evaluation demonstrates compatibility and a concrete advantage for
supported agents. Such work should include a threat model and measurable goals.
