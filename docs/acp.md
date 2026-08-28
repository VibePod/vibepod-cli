# Editor integration (ACP mode)

VibePod can act as an [Agent Client Protocol](https://agentclientprotocol.com/)
(ACP) adapter. ACP is an editor-agnostic, JSON-RPC-based protocol: any editor
or client with ACP support can launch `vp run <agent> --acp` as a subprocess
and embed the containerized agent directly in its AI panel — instead of only
the integrated terminal. Everything that makes a VibePod run what it is —
container isolation, profiles, project overlays, the MITM proxy and local
metric collection — stays active.

## Supported agents

Nine agents ship an ACP adapter command. They split into two kinds, which
differ in what has to happen before the first JSON-RPC frame:

| Agent      | Adapter                                     |
| ---------- | ------------------------------------------- |
| `opencode` | `opencode acp` — built into the CLI         |
| `copilot`  | `copilot --acp --stdio` — built into the CLI |
| `auggie`   | `auggie --acp` — built into the CLI          |
| `jcode`    | `jcode acp` — built into the CLI             |
| `gemini`   | `gemini --experimental-acp` — built in       |
| `qwen`     | `qwen --experimental-acp` — built in         |
| `devstral` | `vibe-acp` — separate binary in the image    |
| `claude`   | `npx @agentclientprotocol/claude-agent-acp`  |
| `codex`    | `npx @agentclientprotocol/codex-acp`         |

The first seven run a binary that is already in the image, so they start
offline and immediately. `claude` and `codex` fetch their adapter over the
network on every launch, which adds startup latency and needs the package
registry reachable through the proxy filter.

Other agents abort with an error listing the supported agents (you can still
provide your own adapter via `agents.<agent>.acp_command` in the config —
that is also how you pin an `npx` adapter to a version).

## Setup

Register `vp` as an external/custom agent server in your editor. The exact
location and format depend on the editor; the general shape is a command plus
arguments. For example, in [Zed](https://zed.dev/docs/ai/external-agents)
(`settings.json`):

```json
{
  "agent_servers": {
    "VibePod Claude": {
      "type": "custom",
      "command": "vp",
      "args": ["run", "claude", "--acp"],
      "env": {}
    }
  }
}
```

Repeat the block for any other supported agent (adjust the `run` argument) if
you want more than one. Make sure `vp` is on the `PATH` your editor inherits
(or use an absolute path).

Then allow your project directory once — under an editor, stdin is a pipe, so
the interactive allow prompt cannot run:

```bash
vp config allow-dir /path/to/your/project
```

Authenticate the agent once interactively too, before the first ACP session:

```bash
vp run <agent>   # sign in, then quit
```

An ACP session cannot log you in. Most adapters expose no authentication
method to the editor at all, and the ones that do point it at a command inside
the container that the editor would run on your host. Credentials persist in
the agent's config dir, so this is a one-time step per agent and profile; skip
it and the thread starts cleanly and then fails on the first prompt.

Open the AI/agent panel in your editor, pick the VibePod thread type, and
start a thread inside your project.

## How it works

`vp run <agent> --acp` starts the container **without a TTY**, attaches before
the entrypoint runs (so the first JSON-RPC frames are not lost), and
demultiplexes the Docker stream: container stdout carries only the
newline-delimited JSON-RPC stream, all VibePod messages go to stderr. The
workspace is mounted a second time onto its own host path so absolute paths
from the editor (session cwd, @-mentions, diffs) resolve identically inside
the container.

When you close the thread, the editor kills the `vp` process; the container
sees stdin EOF, the adapter exits, and `auto_remove` cleans up — no orphaned
containers.

## Windows: run it from WSL2

ACP itself is platform-neutral, and your editor's ACP support is not the
problem — the path-parity mount is. A Linux container's bind *target* has to be
a Linux path, so `C:\Users\you\proj` cannot be mounted onto itself and `--acp`
refuses a Windows workspace path.

WSL2 works today, with no special flags: put the project on the WSL filesystem,
enable Docker Desktop's WSL integration, install `vp` in the distro, and open
the project as a **remote WSL project** in your editor (in Zed:
`projects: open folder in wsl`). The editor then spawns `vp` inside the distro,
every path on both sides is POSIX, and the parity mount lines up.

```json
{
  "agent_servers": {
    "VibePod Claude": {
      "type": "custom",
      "command": "/home/you/.local/bin/vp",
      "args": ["run", "claude", "--acp", "-w", "/home/you/proj"],
      "env": {}
    }
  }
}
```

Use an absolute path to `vp` (the spawn environment is not a login shell) and
pass `-w` explicitly: VibePod picks the workspace from `--workspace` at launch
and never reads the cwd the ACP client sends, and the spawn cwd is not
guaranteed to be your project.

!!! warning "Do not bridge a Windows-side project through `wsl.exe`"

    Running a Windows-native editor against a Windows-side project with
    `"command": "wsl.exe"` looks like it works and then silently misbehaves:
    the editor sends `C:\dev\proj` while VibePod mounts `/mnt/c/dev/proj`, and
    nothing translates between them. The path starts with `/`, so the guard
    above does not catch it. Keep the project, the editor's remote server and
    `vp` all on the Linux side.

## Limitations

- The workspace host path must not collide with container-reserved paths
  (`/workspace`, `/config`, `/claude`, `/qwen`, `/etc`, `/usr`,
  `/tmp/.X11-unix` and the agent's config mount); `--acp` aborts with a clear
  message if it does.
- `--acp` cannot be combined with `--detach`; the ACP client owns the process
  lifetime.
- `--ikwid` is ignored — permissions are negotiated by the editor over ACP.

## Debugging

VibePod's diagnostics (e.g. a missing `vp config allow-dir`) go to stderr and
show up in the editor's ACP logs. Zed, for example, exposes them via
`dev: open acp logs` from the command palette.
