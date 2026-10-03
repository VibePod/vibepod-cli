# VP-152: workerd for sandboxing agent work

Decision: **no-go**. Disposition: **closed with analysis; no runtime integration**.
Reviewed on 2026-10-03. No narrower use currently justifies the implementation
and maintenance cost. This is the repository decision record for VP-152.

## Execution model and local operation

[workerd](https://github.com/cloudflare/workerd) is an event-driven
JavaScript/WebAssembly server runtime using V8 isolates and Workers APIs.
It serves Workers locally with `workerd serve config.capnp`; a Cloudflare account
is not needed. Configuration uses Cap'n Proto, and `workerd compile` can bundle
configuration and source into a standalone executable.
[Configuration reference](https://github.com/cloudflare/workerd/blob/main/src/workerd/server/workerd.capnp).

TypeScript needs a build step to JavaScript, normally provided by
[Wrangler](https://developers.cloudflare.com/workers/languages/typescript/).
Python runs as CPython compiled to Wasm through Pyodide inside an isolate;
local development tooling prepares its runtime and dependencies. It is not the
host's Python environment or a general native-package runtime.
[Python execution and local development](https://developers.cloudflare.com/workers/languages/python/how-python-workers-work/).

Workers cannot launch arbitrary native executables, containers, or shell
processes. Partial Node.js compatibility does not make workerd a full Node
runtime. VibePod's agent commands, interactive terminals, workspace mounts,
Git, compilers, and subprocess tools therefore cannot be moved into isolates.
See the [unimplemented subprocess APIs](https://github.com/cloudflare/workerd/blob/main/src/node/child_process.ts),
[Node.js compatibility](https://developers.cloudflare.com/workers/runtime-apis/nodejs/),
and VibePod's launch contracts in `src/vibepod/core/agents.py`,
`src/vibepod/commands/run.py`, and `src/vibepod/commands/task.py`.

Capabilities can expose specific services or disk directories without granting
ambient host access. However, **public internet access is enabled by default**:
an offline script runner must explicitly restrict the `internet` service or
replace `globalOutbound`, and withhold disk, network, and credential bindings.
[Configuration reference](https://github.com/cloudflare/workerd/blob/main/src/workerd/server/workerd.capnp).

Upstream explicitly warns that **workerd alone is not a hardened sandbox**.
Potentially malicious agent-generated code still requires an outer secure
sandbox, such as a VM; isolates do not replace VibePod's runtime boundary.
[Upstream security warning](https://github.com/cloudflare/workerd#warning-workerd-is-not-a-hardened-sandbox).

## Platforms and packaging

Upstream tests Linux and macOS on x86-64 and arm64, and Windows on x86-64.
Prebuilt binaries ship through npm (`npx workerd`); Linux requires glibc 2.35+,
and macOS requires 13.5+ and Xcode command line tools. CPU requirements include
SSE4.2/CLMUL on x86-64 or CRC on arm64. A stock musl/Alpine agent image cannot
simply consume the Linux binary. Source builds use Bazel and platform compiler
toolchains; they would add substantial build maintenance.
[Installation requirements](https://github.com/cloudflare/workerd#running-workerd),
[platform packages](https://github.com/cloudflare/workerd/blob/main/npm/workerd/package.json).

## Candidate VibePod uses and effort

These are engineering estimates from the current architecture, not measured
implementation times. A production integration would also need opt-in
configuration, pinned packaging, documentation, and platform CI.

| Candidate | Concrete value | Effort and fit |
| --- | --- | --- |
| Pure JS/Wasm script tool | Transform JSON, parse responses, or compute results without workspace or credential access. | Medium: introduce a tool/MCP service, request/result protocol, capability configuration, process lifecycle, time/memory/output limits, and adversarial tests inside an outer sandbox. VibePod launches third-party CLIs rather than mediating their individual tool calls; adoption would require agent-specific wiring. No existing requirement warrants this service. |
| Proxy filter Worker | Evaluate custom request policies or redact metadata through a narrow HTTP binding. | High: add an RPC hop or port policy logic, preserve per-container identity, live policy updates, fail-closed behavior, streaming, and logging. The existing MITM proxy already provides declarative allow/deny rules; replacing it also needs TLS interception, CA management, and SQLite analytics compatibility. A programmable HTTP proxy is not a drop-in MITM replacement. |
| Local HTTP component | Host a small trusted webhook or response transformation service with explicit backend bindings. | Medium: package and supervise another runtime and define a new service contract. Existing CLI, configuration, and analytics components have no identified service that needs this capability; no demonstrated benefit offsets the dependency. |

The proxy assessment follows the current [proxy contract](../configuration.md#the-built-in-proxy),
`src/vibepod/core/docker.py::ensure_proxy`, and
`src/vibepod/core/proxy_filter.py`; the proxy implementation lives in the
[separate proxy repository](https://github.com/VibePod/vibepod-proxy).

## Outcome

Close VP-152 as **no-go**: workerd cannot run the supported agent toolchains,
does not independently provide the required security boundary, and the narrower
uses add integration cost without an unmet VibePod need. No setting, runtime
dependency, or experimental implementation is added. Reconsider in a separate
task if a specific agent needs a pure JS/Wasm tool service with a defined API,
outer sandbox, resource budget, and capability policy, or if programmable proxy
extensions become a concrete requirement.

Validation: `mkdocs build --strict` and `git diff --check` passed. This is a
documentation-only decision; no runtime prototype or performance benchmark was
needed to reject the candidates.
