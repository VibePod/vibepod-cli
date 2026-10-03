# Process sandbox (Sydbox)

!!! warning "Experimental, opt-in"
    The sandbox is off by default, and `vp run` and `vp task` behave exactly
    as before unless you enable it. It adds a second layer **inside** the agent
    container. It does not replace the container, and it does not make an
    untrusted agent safe to run on its own.

[Sydbox](https://gitlab.exherbo.org/sydbox/sydbox) (`syd`) is a Linux
application sandbox. It intercepts every system call of a process tree through
seccomp user notification and enforces rule-based policies on paths, network
addresses and executables. It needs no root, no kernel module and no eBPF.

With `--sandbox syd`, VibePod runs the agent process under syd with a policy it
renders for each launch:

- **Writes are confined** to the workspace, the agent's config/home mounts,
  writable volumes and temp directories. The image's system directories
  (`/usr`, `/etc`, `/opt`, `/bin`, `/lib`, `/var`, ...) are read-only to the
  agent, even when it runs as root.
- **Host-executed git metadata is protected.** `.git/hooks/`, `.git/config`
  and the `.git` entry itself cannot be written, created, renamed, symlinked,
  chmodded or deleted in the workspace. The workspace is a bind mount of your
  host directory, so these files are the shortest path from a misbehaving agent
  to code running **on your host** the next time you use git there: a planted
  hook, or `core.hooksPath` / `core.fsmonitor` in the config.
- **Extra rules** from `sandbox.syd.rules` are appended, for example to block
  executables or network destinations.
- **Every denial is logged** as a JSON line that names the path, the system
  call and the rule that would allow it.

## Setup

1. Download a **static** syd release for your container architecture from
   [distfiles.exherbo.org/#sydbox](https://distfiles.exherbo.org/#sydbox/)
   (`syd-<version>-x86_64-pc-linux-gnu.tar.xz` or
   `syd-<version>-aarch64-unknown-linux-gnueabi.tar.xz`). Verify its checksum,
   and its signature against [syd.asc](https://distfiles.exherbo.org/sydbox/syd.asc):

    ```bash
    V=3.53.0 A=x86_64-pc-linux-gnu
    curl -LO https://distfiles.exherbo.org/sydbox/syd-$V-$A.tar.xz
    curl -LO https://distfiles.exherbo.org/sydbox/syd-$V-$A.tar.xz.sha512sum
    sha512sum -c syd-$V-$A.tar.xz.sha512sum
    tar -xJf syd-$V-$A.tar.xz && mkdir -p ~/.local/lib/vibepod
    cp syd-$V-$A/bin/syd ~/.local/lib/vibepod/syd
    ```

    Distribution packages (`pacman -S syd`, ...) are dynamically linked
    against the host's libc and will not start inside agent images, so
    VibePod rejects them.

2. Point VibePod at the binary in the global config
   (`~/.config/vibepod/config.yaml`):

    ```yaml
    sandbox:
      syd:
        binary: ~/.local/lib/vibepod/syd
    ```

3. Check the setup:

    ```bash
    vp sandbox check
    ```

## Enabling it

Per run, for `vp run` and `vp task create`:

```bash
vp run claude --sandbox syd
vp task create codex "fix the failing test" --sandbox syd
```

Per project, in `.vibepod/config.yaml`:

```yaml
sandbox:
  type: syd # none (default) | syd
```

`--sandbox none` turns it off for one run in a project that enables it, and
`VP_SANDBOX=syd` enables it from the environment. If the sandbox is requested
but the binary is missing, unreadable, not executable, dynamically linked or
not an ELF file, or the Linux host kernel is older than 5.19, the launch stops
before anything is started. The error says what to fix.

Running containers are labelled `vibepod.sandbox=syd`.

## The policy

Preview the policy for an agent with `vp sandbox policy <agent>`. The launch
policy also covers the agent's extra volume mounts and, with `--acp`, the host
path the workspace is bound to. VibePod stores each distinct policy under
`~/.config/vibepod/sandbox/policies/` and mounts it, together with the binary,
read-only at `/opt/vibepod-sandbox/` in the container.

Some syd defaults are relaxed because agents cannot run without these changes.
The policy says so in comments:

| Setting                        | Why                                                                                                                                                                                    |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `passenv+*`                    | syd scrubs "unsafe" variables by default, including `ANTHROPIC_API_KEY`, `HTTPS_PROXY`, `NODE_EXTRA_CA_CERTS` and `CLAUDE_CONFIG_DIR`. VibePod sets the container environment itself. |
| `include_profile nomem`        | Node/V8 and Bun/JavaScriptCore JIT-compile and need writable+executable memory. Claude Code dies with `SIGSYS` without it.                                                             |
| `include_profile nopie`        | Some agent binaries (Bun-compiled) are not position independent.                                                                                                                       |
| `sandbox/lock:off`             | syd's Landlock layer would need its own exec/read rule set. Path rules are enforced through seccomp instead.                                                                           |
| `allow/all+/***`, network open | Reads, exec and network stay as they are without the sandbox. Writes are confined.                                                                                                     |

Add rules with `sandbox.syd.rules`. In syd the last matching rule wins, and
these rules come last:

```yaml
sandbox:
  syd:
    log_level: warn # error silences denial lines
    rules:
      # Block the cloud metadata endpoint
      - deny/net/connect+169.254.169.254/32!0-65535
      # Block selected executables
      - deny/exec+/usr/bin/curl
      - deny/exec+/usr/bin/wget
      # Keep another path writable
      - allow/wpath,cpath,dpath,tpath,fattr,symlink+/opt/cache/***
```

See [syd(2)](https://man.exherbo.org/syd.2.html) for the rule language.

## Violation reports

syd writes one JSON object per denial to the agent's stderr:

```json
{"ctx":"access","cap":"write","act":"deny","sys":"openat","path":"/workspace/.git/hooks/pre-commit",
 "cmd":"/bin/sh -c ...","pid":2273,"uid":1000,"time":"20261003T212323Z",
 "tip":"configure `allow/write+/workspace/.git/hooks/pre-commit'"}
```

For `vp task` and `vp run -d` the lines land in the container logs (`vp task
logs`, `docker logs`). In an interactive `vp run` they print into the agent's
terminal. Set `sandbox.syd.log_level: error` to keep the TUI clean. The agent
itself only sees `Permission denied` (`EACCES`) or `Connection refused`.

## Limitations

- **Linux kernel 5.19 or newer.** On Docker Desktop, Colima or Podman machine
  the VM's kernel counts. VibePod cannot check that kernel from the host, so
  an old one shows up as syd failing at container start. The binary has to
  match the container architecture (x86_64 or aarch64).
- **Overhead.** Every intercepted system call is a round trip to the syd
  supervisor. Measured in a 2-CPU container with syd 3.53 and 3.59 under this
  policy: a Python test suite ran about 1.7× slower (3.0 s → 5.2 s), while
  syscall-heavy work ran about 5–12× slower (`git status`/`git log` loop
  0.1 s → 0.5 s, `find /usr` 0.07 s → 0.8 s). Expect large package installs
  and repository-wide scans to slow down noticeably. LLM round trips, where
  most agent time goes, are not affected.
- **The agent cannot initialise or reconfigure the workspace repository.**
  `git init`, `git config` and installing hooks in the workspace root are
  denied. Run them on the host. Nested repositories and other host-read files
  (`.envrc`, `.vscode/`, `Makefile`, ...) are not protected by default.
- **Network is not restricted by default.** Proxy filtering still applies only
  to clients that use the proxy. Add `deny/net/connect` rules to block
  destinations.
- **Only the agent command is sandboxed.** The image entrypoint runs first as
  root to map your UID and drop privileges. syd blocks those `setgroups` and
  `setregid` transitions, which is also why VibePod does not use `syd-oci` to
  sandbox the whole container. `vp attach` / `docker exec` shells, and the
  `init` commands from `agents.<agent>.init`, run outside syd.
- **Policy file ownership.** Recent syd releases (3.59) only load a policy
  owned by root or by the sandboxed user. VibePod's images run the agent under
  your host UID, which owns the file. On rootless Podman (`keep-id`, agent as
  UID 0) a recent syd may refuse the policy. syd 3.53 accepts it.
- **Not yet exercised with every agent image.** The policy was verified
  against syd 3.53 and 3.59 with the Claude Code binary, git, Python and a
  shell. Other agents may hit a denial (the log line's `tip` names the rule
  that would allow it) or a syd hardening default that needs relaxing.
- **Same kernel.** syd hardens the process. It is not a VM boundary: a kernel
  exploit escapes both syd and the container.

## Analysis and decision

This section records the evaluation behind the feature: VP-150, integration
shape *process sandbox* from VP-143.

**What syd is.** syd 3 is a Rust rewrite of sydbox 2, developed for Exherbo
Linux by Ali Polatel and presented at FrOSCon 2026 ("Sydbox: A highly
paranoid, innovative sandbox for Linux"). It is GPL-3.0-only and has been
released about monthly (3.55 to 3.59 between June and September 2026). It
ships signed static builds for eight architectures and is packaged for Arch,
Alpine, Gentoo and Exherbo. Most of the code comes from one primary
maintainer, which is a bus-factor risk. Defaults get stricter between
releases (for example the policy-ownership check), so VibePod keeps its policy
to rules that both 3.53 (the newest static build) and 3.59 accept, and the
test suite runs it against a real binary when `VP_TEST_SYD_BINARY` is set.

**Runs in containers: yes.** syd 3.53 (static) and 3.59 were run inside an
unprivileged container: seccomp filter active, no `CAP_SYS_ADMIN`, the syd
process as root and as an unprivileged UID. Path, exec and network rules,
denial logging, a TTY, git, Python and the Claude Code binary all worked.
Docker's default seccomp profile permits the `seccomp(2)` user-notification
setup syd needs. No extra capability or `--security-opt` is required.

**Two integration shapes were evaluated:**

- `syd-oci`, an OCI runtime built on youki and registered with
  Docker/Podman, sandboxes the whole container. Its built-in `oci` profile
  includes `trace`, which is dry-run mode: without a policy file in
  `/etc/syd/oci` it only logs and enforces nothing. It is also not in the
  static release, it needs root to register with the engine, and it is
  Linux-host only. Worst of all, the agent images' root entrypoint (`su` /
  `setpriv` privilege drop) fails under syd. **Rejected.**
- **Wrap the agent command** after the entrypoint has dropped privileges,
  with a host-provided static binary and a VibePod-rendered policy mounted
  read-only. This needs no image or engine changes and works wherever the
  engine's Linux kernel is new enough. **Chosen.**

**Compatibility with VibePod features.** The proxy, its CA and the injected
credentials reach the agent through `passenv+*` (verified). The rest is
compatible by design and covered by unit tests, not yet by a live run of each
agent image:

- Provider wrappers run inside syd.
- `init` commands run before syd.
- ACP: the host-path workspace binds get the same git protection.
- herdr and X11 unix sockets are allowed by `allow/net/connect+/***`.
- Custom volumes: `rw` targets stay writable.
- Rootless Podman works, subject to the ownership note above.

**Isolation gained** over the container alone: protection of the host-mounted
workspace against host-executed persistence (git hooks and config), a
read-only image even for root agents, per-project exec/network deny rules, and
an audit trail of denied operations. **Not gained:** network confinement by
default, protection of other host-read files, and protection against kernel
exploits.

**Compared with bubblewrap.** bubblewrap builds a mount namespace and needs
unprivileged user namespaces, or `CAP_SYS_ADMIN`, both of which Docker's
default seccomp profile blocks. It can only express "this path is
read-only/hidden", has no per-file deny inside a writable tree (the
`.git/hooks` case), no network address rules, no exec deny lists and no
denial log. It has almost no runtime overhead, though, because the kernel
enforces it once at setup. syd fits VibePod's needs (fine-grained rules
inside an already-unprivileged container) better, at the cost of per-syscall
overhead and a stricter, faster-moving upstream.

**Decision: go**, as an opt-in, experimental sandbox type with the default
unchanged. Possible follow-ups:

- Ship syd in the `vibepod-agents` images so no host download is needed.
- Default network rules that allow only the VibePod proxy and DNS.
- Collect denial lines into the VibePod logs database.
